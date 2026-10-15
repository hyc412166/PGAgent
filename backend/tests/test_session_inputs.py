"""验证 steer 的真实上下文边界和持久 follow-up 队列，不访问外部模型。"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.agent import AgentRuntime, ModelTurn, RuntimeConfig
from src.api.runtime import router
from src.attachments.storage import AttachmentToolStore
from src.config import settings
from src.context.assembly import FilesystemArtifactStore
from src.context.compaction import _prepare_session_history
from src.persistence import database
from src.persistence.models import ChatMessage, Run, Session, SessionInput
from src.runs.service import coordinator
from src.sessions.delivery import stage_user_turn
from src.sessions.inputs import consume_steering, dispatch_follow_up
from src.tools import create_default_registry


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    database.configure_database(f"sqlite:///{(tmp_path / 'inputs.db').as_posix()}")
    database.init_db()
    launches = []
    monkeypatch.setattr(coordinator, "launch", lambda run_id, resume=False: launches.append(run_id) or True)
    monkeypatch.setattr(coordinator, "schedule_session_inputs", lambda _session_id: None)
    with database.SessionLocal() as db:
        session = Session(title="输入测试", agent_id=database.DEFAULT_AGENT_ID, workspace_id=database.DEFAULT_WORKSPACE_ID)
        db.add(session)
        db.flush()
        _, _, run = stage_user_turn(db, session_id=session.id, agent_id=database.DEFAULT_AGENT_ID,
            workspace_id=database.DEFAULT_WORKSPACE_ID, content="original", mode="auto", client_message_id="initial")
        run.status = "acting"
        db.commit()
        ids = session.id, run.id
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, *ids, launches
    database.Base.metadata.drop_all(database.engine)


def submit(inbox, content="补充", mode="steer", key="input-1", files=None):
    client, session_id, run_id, _ = inbox
    payload = {"content": content, "mode": mode, "idempotency_key": key, "expected_run_id": run_id}
    if files:
        return client.post(f"/api/sessions/{session_id}/inputs", data={"payload": json.dumps(payload)}, files=files)
    return client.post(f"/api/sessions/{session_id}/inputs", json=payload)


def set_status(inbox, status, reason=None):
    with database.SessionLocal() as db:
        run = db.get(Run, inbox[2])
        run.status, run.stop_reason = status, reason
        db.commit()


def test_retry_cancel_and_closed_target(inbox):
    first = submit(inbox)
    assert first.status_code == 202
    assert submit(inbox).json()["id"] == first.json()["id"]
    assert submit(inbox, content="different").status_code == 409
    client, sid, rid, _ = inbox
    assert client.delete(f"/api/sessions/{sid}/inputs/{first.json()['id']}").status_code == 200
    state = {"messages": [], "transcript_delta": [], "context": {}}
    assert consume_steering(rid, state, True) == []
    assert submit(inbox, key="late").status_code == 409
    with database.SessionLocal() as db:
        assert db.get(Run, rid).input_closed
        assert len(list(db.scalars(select(SessionInput)))) == 1


def test_simultaneous_retries_create_one_input(inbox):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: submit(inbox), range(2)))
    assert {r.status_code for r in responses} == {202}
    assert len({r.json()["id"] for r in responses}) == 1


def test_cross_session_and_child_targets_rejected(inbox):
    client, sid, rid, _ = inbox
    with database.SessionLocal() as db:
        other = Session(title="其他会话")
        db.add(other)
        db.flush()
        child = Run(session_id=sid, status="acting")
        db.add(child)
        db.commit()
        other_id, child_id = other.id, child.id
    for session_id, target in [(other_id, rid), (sid, child_id)]:
        response = client.post(f"/api/sessions/{session_id}/inputs", json={
            "content": "wrong", "mode": "steer", "idempotency_key": "wrong", "expected_run_id": target,
        })
        assert response.status_code == 409


@pytest.mark.parametrize("status,reason", [
    ("acting", None), ("awaiting_approval", None), ("stopped", "waiting_background"),
    ("stopped", "delegated_child_waiting_event"), ("stopped", "delegated_child_awaiting_approval"),
    ("stopped", "user_interrupted"), ("failed", None),
])
async def test_queue_waits_for_normal_completion(inbox, status, reason):
    queued = submit(inbox, mode="follow_up").json()
    set_status(inbox, status, reason)
    await dispatch_follow_up(coordinator, inbox[1])
    assert inbox[3] == []
    with database.SessionLocal() as db:
        assert db.get(SessionInput, queued["id"]).status == "queued"
        assert [m.content for m in db.scalars(select(ChatMessage))] == ["original"]


async def test_queue_fifo_and_explicit_start_after_stop(inbox):
    first = submit(inbox, content="first", mode="follow_up").json()
    second = submit(inbox, content="second", mode="follow_up", key="input-2").json()
    set_status(inbox, "completed")
    await dispatch_follow_up(coordinator, inbox[1])
    await dispatch_follow_up(coordinator, inbox[1])
    assert len(inbox[3]) == 1
    with database.SessionLocal() as db:
        assert db.get(SessionInput, first["id"]).status == "delivered"
        assert db.get(SessionInput, second["id"]).status == "queued"
        run = db.get(Run, inbox[3][0])
        run.status, run.stop_reason = "stopped", "user_interrupted"
        db.commit()
    await dispatch_follow_up(coordinator, inbox[1])
    assert len(inbox[3]) == 1
    response = inbox[0].post(f"/api/sessions/{inbox[1]}/inputs/{second['id']}/start")
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"
    assert len(inbox[3]) == 2


def test_queue_promotion_and_failed_pending_survive_reload(inbox):
    queued = submit(inbox, mode="follow_up").json()
    path = f"/api/sessions/{inbox[1]}/inputs/{queued['id']}/steer"
    response = inbox[0].post(path, json={"expected_run_id": inbox[2]})
    assert response.json()["status"] == "pending"
    set_status(inbox, "stopped", "interrupted_restart")
    restored = inbox[0].get(f"/api/sessions/{inbox[1]}/inputs").json()
    assert restored[0]["content"] == "补充"
    assert restored[0]["status"] == "failed"
    assert restored[0]["error"]


async def test_queued_attachment_hidden_until_delivery_and_retry_bytes(inbox):
    files = [("files", ("note.txt", b"secret queued content", "text/plain"))]
    result = submit(inbox, content="", mode="follow_up", files=files)
    assert result.status_code == 202
    assert submit(inbox, content="", mode="follow_up", files=files).json()["id"] == result.json()["id"]
    assert submit(inbox, content="", mode="follow_up", files=[("files", ("note.txt", b"different", "text/plain"))]).status_code == 409
    ref = result.json()["attachments"][0]
    store = AttachmentToolStore(inbox[1], FilesystemArtifactStore(settings.data_dir / "artifacts" / inbox[1]))
    assert store.content(ref["id"]) is None
    set_status(inbox, "completed")
    await dispatch_follow_up(coordinator, inbox[1])
    assert store.content(ref["id"])[1] == b"secret queued content"


async def test_steer_during_final_response_keeps_same_run_and_transcript_order(inbox, tmp_path):
    calls = []
    async def model(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            assert submit(inbox, content="改成中文").status_code == 202
            return ModelTurn(content="first candidate")
        assert messages[-1]["content"] == "改成中文"
        return ModelTurn(content="最终答案")

    events = []
    runtime = AgentRuntime(model_call=model, tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        input_provider=lambda state, finishing: consume_steering(inbox[2], state, finishing),
        event_sink=events.append, config=RuntimeConfig(max_steps=5))
    outcome = await runtime.run(system_prompt="You are helpful", recent_messages=[{"role": "user", "content": "original"}])
    assert outcome.status == "completed"
    assert outcome.output == "最终答案"
    assert len(calls) == 2
    assert [e["type"] for e in events].count("run_completed") == 1
    with database.SessionLocal() as db:
        rows = list(db.scalars(select(ChatMessage).order_by(ChatMessage.sequence)))
        assert [m.content for m in rows] == ["original", "first candidate", "改成中文"]
        assert db.get(Run, inbox[2]).input_closed
        history = _prepare_session_history(db, db.get(Session, inbox[1]))
        assert [m["content"] for m in history][-2:] == ["first candidate", "改成中文"]
    assert inbox[3] == []


async def test_steer_image_is_model_input_before_next_request(inbox, tmp_path):
    result = submit(inbox, content="图片", files=[("files", ("image.png", b"png test", "image/png"))])
    assert result.status_code == 202
    async def model(messages, **_kwargs):
        parts = messages[-1]["content"]
        assert parts[1]["type"] == "pgagent_image_ref"
        assert parts[1]["attachment_id"] == result.json()["attachments"][0]["id"]
        return ModelTurn(content="done")
    runtime = AgentRuntime(model_call=model, tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        input_provider=lambda state, finishing: consume_steering(inbox[2], state, finishing))
    outcome = await runtime.run(system_prompt="helpful", recent_messages=[{"role": "user", "content": "original"}])
    assert outcome.status == "completed"
    with database.SessionLocal() as db:
        assert db.get(SessionInput, result.json()["id"]).status == "delivered"


async def test_steer_accepted_during_model_preparation_is_in_next_request(inbox, tmp_path):
    requests = []
    def sink(event):
        if event["type"] == "model_step_started" and not requests:
            assert submit(inbox, content="准备期间补充").status_code == 202
    async def model(messages, **kwargs):
        requests.append(messages)
        return ModelTurn(content="done")
    runtime = AgentRuntime(model_call=model, tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        input_provider=lambda state, finishing: consume_steering(inbox[2], state, finishing), event_sink=sink)
    await runtime.run(system_prompt="helpful", recent_messages=[{"role": "user", "content": "original"}])
    assert requests[0][-1]["content"] == "准备期间补充"
    assert len(requests) == 1


async def test_launch_failure_is_retryable_with_original_attachments(inbox, monkeypatch):
    result = submit(inbox, content="", mode="follow_up", files=[("files", ("note.txt", b"keep file", "text/plain"))]).json()
    set_status(inbox, "completed")
    monkeypatch.setattr(coordinator, "launch", lambda *args, **kwargs: False)
    await dispatch_follow_up(coordinator, inbox[1])
    with database.SessionLocal() as db:
        item = db.get(SessionInput, result["id"])
        assert item.status == "failed"
        assert item.error
    monkeypatch.setattr(coordinator, "launch", lambda run_id, **kwargs: inbox[3].append(run_id) or True)
    response = inbox[0].post(f"/api/sessions/{inbox[1]}/inputs/{result['id']}/start")
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"
    assert response.json()["attachments"] == result["attachments"]
    assert len(inbox[3]) == 1


def test_failed_attachment_only_steer_can_start_new_turn(inbox):
    result = submit(inbox, content="", files=[("files", ("note.txt", b"keep file", "text/plain"))]).json()
    set_status(inbox, "stopped", "user_interrupted")
    inbox[0].get(f"/api/sessions/{inbox[1]}/inputs")
    response = inbox[0].post(f"/api/sessions/{inbox[1]}/inputs/{result['id']}/start")
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"
    assert response.json()["attachments"] == result["attachments"]


def test_repeated_assistant_text_between_two_steers_keeps_order(inbox):
    submit(inbox, content="first steer")
    state = {"messages": [], "transcript_delta": [{"role": "assistant", "content": "same text"}], "context": {}}
    consume_steering(inbox[2], state, False)
    submit(inbox, content="second steer", key="input-2")
    state["transcript_delta"] = [{"role": "assistant", "content": "same text"}]
    consume_steering(inbox[2], state, False)
    with database.SessionLocal() as db:
        rows = list(db.scalars(select(ChatMessage).order_by(ChatMessage.sequence)))
        assert [m.content for m in rows] == ["original", "same text", "first steer", "same text", "second steer"]


def test_explicit_start_cannot_skip_queue_head(inbox):
    submit(inbox, mode="follow_up")
    second = submit(inbox, content="second", mode="follow_up", key="input-2").json()
    set_status(inbox, "stopped", "user_interrupted")
    response = inbox[0].post(f"/api/sessions/{inbox[1]}/inputs/{second['id']}/start")
    assert response.status_code == 409
    assert inbox[3] == []
