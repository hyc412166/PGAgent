from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import database
from app.api.runtime import router
from app.database import (
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    Agent,
    Approval,
    Base,
    ChatMessage,
    DraftLaunch,
    Run,
    RunEvent,
    Session,
    TeamTask,
    AgentMessage,
    Workspace,
    configure_database,
    init_db,
)
from app.services.run_service import coordinator
from app.services.run_stream import run_stream_broker


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, list]]:
    configure_database(f"sqlite:///{(tmp_path / 'runtime-api.db').as_posix()}")
    init_db()
    launched: dict[str, list] = {"calls": []}
    monkeypatch.setattr(
        coordinator,
        "launch",
        lambda run_id, resume=False: launched["calls"].append((run_id, resume)) or True,
    )
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client, launched
    Base.metadata.drop_all(bind=database.engine)


def _seed() -> tuple[str, str, str]:
    with database.SessionLocal() as db:
        workspace = Workspace(name="Demo", root_path="C:/demo")
        db.add(workspace)
        db.flush()
        agent = Agent(name="Builder", workspace_id=workspace.id)
        db.add(agent)
        db.flush()
        session = Session(title="Chat", workspace_id=workspace.id, agent_id=agent.id)
        db.add(session)
        db.commit()
        return workspace.id, agent.id, session.id


def test_launch_session_run_persists_message_and_returns_immediately(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    _workspace_id, _agent_id, session_id = _seed()
    response = test_client.post(f"/api/sessions/{session_id}/run", json={"content": "读取文件"})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert response.json()["status"] == "received"
    assert launched["calls"] == [(run_id, False)]
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.mode == "auto"
        assert run.agent_id == DEFAULT_AGENT_ID
        session = db.get(Session, session_id)
        assert session is not None and session.agent_id == DEFAULT_AGENT_ID
        assert db.query(database.ChatMessage).filter_by(session_id=session_id).one().content == "读取文件"


def test_second_active_run_is_rejected(client: tuple[TestClient, dict[str, list]]) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        db.add(Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="acting"))
        db.commit()
    response = test_client.post(f"/api/sessions/{session_id}/run", json={"content": "again"})
    assert response.status_code == 409


def test_run_stream_sends_current_state_replay_and_terminal_with_stable_ids(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            status="completed",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    started = run_stream_broker.publish(run_id, {"type": "context_prepared"})
    terminal = run_stream_broker.publish(run_id, {"type": "run_completed", "output": "done"})

    response = test_client.get(
        f"/api/runs/{run_id}/stream",
        headers={"Last-Event-ID": started["event_id"]},
    )

    assert response.status_code == 200
    assert "event: run_state" in response.text
    assert '"status":"completed"' in response.text
    assert "context_prepared" not in response.text
    assert "event: run_completed" in response.text
    assert f"id: {terminal['event_id']}" in response.text
    assert run_stream_broker.subscriber_count(run_id) == 0


def test_approval_decision_resumes_only_when_approved(client: tuple[TestClient, dict[str, list]]) -> None:
    test_client, launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="awaiting_approval")
        db.add(run)
        db.flush()
        approval = Approval(run_id=run.id, tool_name="write_file", arguments={"path": "a.txt"})
        snapshot = RunEvent(
            run_id=run.id,
            event_type="runtime_snapshot",
            payload={
                "status": "awaiting_approval",
                "pending_approval": {
                    "id": "call-1",
                    "tool_name": "write_file",
                    "arguments": {"path": "a.txt"},
                },
            },
        )
        db.add_all([approval, snapshot])
        db.commit()
        approval_id, run_id = approval.id, run.id

    response = test_client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert launched["calls"] == [(run_id, True)]


def test_approval_decision_is_restored_when_resume_cannot_be_scheduled(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="awaiting_approval")
        db.add(run)
        db.flush()
        approval = Approval(
            run_id=run.id,
            tool_name="write_file",
            arguments={"path": "a.txt"},
            reason="write access",
        )
        snapshot = RunEvent(
            run_id=run.id,
            event_type="runtime_snapshot",
            payload={
                "status": "awaiting_approval",
                "pending_approval": {
                    "id": "call-1",
                    "tool_name": "write_file",
                    "arguments": {"path": "a.txt"},
                },
            },
        )
        db.add_all([approval, snapshot])
        db.commit()
        approval_id, run_id = approval.id, run.id

    monkeypatch.setattr(coordinator, "launch", lambda _run_id, resume=False: False)
    response = test_client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"})

    assert response.status_code == 503
    assert response.json()["detail"] == "续跑任务暂时无法启动，请重试审批"
    with database.SessionLocal() as db:
        restored_approval = db.get(Approval, approval_id)
        restored_run = db.get(Run, run_id)
        assert restored_approval is not None
        assert restored_approval.status == "pending"
        assert restored_approval.reason == "write access"
        assert restored_approval.decided_at is None
        assert restored_run is not None and restored_run.status == "awaiting_approval"


def test_rejecting_delegated_child_approval_settles_its_task_and_parent_audit(
    client: tuple[TestClient, dict[str, list]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client, launched = client
    continuation_events: list[dict[str, object] | None] = []
    monkeypatch.setattr(
        coordinator,
        "launch_parent_continuation_if_queued",
        lambda event: continuation_events.append(event) or True,
    )
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        parent = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=DEFAULT_AGENT_ID,
            status="stopped",
            stop_reason="delegated_child_awaiting_approval",
        )
        child = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            status="awaiting_approval",
        )
        db.add_all([parent, child])
        db.flush()
        task = TeamTask(
            team_id=f"run:{parent.id}",
            title="delegated child",
            status="in_progress",
            assignee_agent_id=agent_id,
            lease_owner=f"run:{parent.id}",
            result={
                "child_run_id": child.id,
                "status": "awaiting_approval",
                "binding": {
                    "model_connection_id": "child-connection",
                    "provider": "openai_compatible",
                    "model_id": "child-model",
                    "thinking_level": "high",
                    "permission_mode": "smart",
                    "allowed_tool_names": ["read"],
                    "skill_ids": [],
                },
            },
        )
        db.add(task)
        db.flush()
        db.add(AgentMessage(
            team_id=task.team_id,
            task_id=task.id,
            sender_agent_id=DEFAULT_AGENT_ID,
            recipient_agent_id=agent_id,
            message_type="TASK_ASSIGNED",
            payload={"child_run_id": child.id},
            idempotency_key=f"assignment:{task.id}",
        ))
        db.add_all([
            RunEvent(
                run_id=child.id,
                event_type="delegation_link",
                payload={
                    "team_task_id": task.id,
                    "parent_run_id": parent.id,
                    "parent_agent_id": DEFAULT_AGENT_ID,
                    "parent_session_id": session_id,
                },
            ),
            RunEvent(
                run_id=child.id,
                event_type="runtime_snapshot",
                payload={
                    "status": "awaiting_approval",
                    "runtime_binding": {
                        "delegation_version": 1,
                        "agent_id": agent_id,
                        "model_connection_id": "child-connection",
                        "provider": "openai_compatible",
                        "model_id": "child-model",
                        "thinking_level": "high",
                        "permission_mode": "smart",
                        "allowed_tool_names": ["read"],
                        "skill_ids": [],
                    },
                    "pending_approval": {
                        "id": "child-call",
                        "tool_name": "write",
                        "arguments": {"path": "child.txt", "content": "secret"},
                    },
                },
            ),
        ])
        approval = Approval(
            run_id=child.id,
            tool_name="write",
            arguments={"path": "child.txt", "content": "secret"},
        )
        db.add(approval)
        db.commit()
        approval_id, task_id, parent_run_id = approval.id, task.id, parent.id

    response = test_client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"decision": "reject", "reason": "not now"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert launched["calls"] == []
    assert len(continuation_events) == 1
    assert continuation_events[0] is not None
    assert continuation_events[0]["continuation_queued"] is True
    with database.SessionLocal() as db:
        task = db.get(TeamTask, task_id)
        assert task is not None and task.status == "blocked"
        assert task.result["status"] == "stopped"
        assert task.result["binding"]["model_id"] == "child-model"
        assert task.result["binding"]["allowed_tool_names"] == ["read"]
        parent = db.get(Run, parent_run_id)
        assert parent is not None and parent.status == "received"
        messages = list(db.scalars(select(AgentMessage).where(
            AgentMessage.task_id == task_id,
        ).order_by(AgentMessage.created_at.asc())))
        assert [item.message_type for item in messages] == ["TASK_ASSIGNED", "BLOCKED"]
        assert db.scalar(select(RunEvent).where(
            RunEvent.run_id == parent_run_id,
            RunEvent.event_type == "delegated_child_stopped",
        )) is not None
        child_message = next(
            item for item in db.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id))
            if item.extra.get("delegated_child") is True
        )
        assert child_message.extra["delegated_status"] == "stopped"


def test_approval_decision_rejects_run_not_awaiting_approval(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="acting")
        db.add(run)
        db.flush()
        approval = Approval(run_id=run.id, tool_name="write_file", arguments={"path": "a.txt"})
        db.add(approval)
        db.commit()
        approval_id = approval.id

    response = test_client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"})
    assert response.status_code == 409
    assert launched["calls"] == []
    with database.SessionLocal() as db:
        assert db.get(Approval, approval_id).status == "pending"


def test_approval_decision_rejects_mismatch_with_latest_runtime_snapshot(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="awaiting_approval")
        db.add(run)
        db.flush()
        approval = Approval(run_id=run.id, tool_name="write_file", arguments={"path": "approved.txt"})
        snapshot = RunEvent(
            run_id=run.id,
            event_type="runtime_snapshot",
            payload={
                "status": "awaiting_approval",
                "pending_approval": {
                    "id": "call-new",
                    "tool_name": "write_file",
                    "arguments": {"path": "different.txt"},
                },
            },
        )
        db.add_all([approval, snapshot])
        db.commit()
        approval_id = approval.id

    response = test_client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"})
    assert response.status_code == 409
    assert launched["calls"] == []
    with database.SessionLocal() as db:
        assert db.get(Approval, approval_id).status == "pending"


def test_launch_uses_fixed_defaults_when_session_has_no_bindings(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    with database.SessionLocal() as db:
        session = Session(title="Uses defaults")
        db.add(session)
        db.commit()
        session_id = session.id

    response = test_client.post(f"/api/sessions/{session_id}/run", json={"content": "hello"})
    assert response.status_code == 202, response.text
    with database.SessionLocal() as db:
        run = db.get(Run, response.json()["id"])
        assert run is not None
        assert run.agent_id == DEFAULT_AGENT_ID
        assert run.workspace_id == DEFAULT_WORKSPACE_ID
        assert run.mode == "auto"


def _draft_payload(
    *,
    key: str,
    content: str = "Create the project summary",
    title: str = "Create project summary",
    root_path: str | None = None,
    model_connection_id: str | None = None,
) -> dict[str, str]:
    payload = {
        "idempotency_key": key,
        "title": title,
        "content": content,
        "thinking_level": "high",
    }
    if root_path is not None:
        payload["root_path"] = root_path
    if model_connection_id is not None:
        payload["model_connection_id"] = model_connection_id
    return payload


def _draft_resource_counts() -> tuple[int, int, int, int]:
    with database.SessionLocal() as db:
        return (
            db.query(Workspace).count(),
            db.query(Session).count(),
            db.query(ChatMessage).count(),
            db.query(DraftLaunch).count(),
        )


def test_launch_draft_materializes_once_and_retries_without_rescheduling(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    payload = _draft_payload(key="draft-repeat")

    first = test_client.post("/api/drafts/launch", json=payload)
    assert first.status_code == 202, first.text
    first_body = first.json()
    assert first_body["reused"] is False
    assert first_body["workspace"]["id"] == DEFAULT_WORKSPACE_ID

    retry = test_client.post("/api/drafts/launch", json=payload)
    assert retry.status_code == 200, retry.text
    retry_body = retry.json()
    assert retry_body["reused"] is True
    assert retry_body["session"]["id"] == first_body["session"]["id"]
    assert retry_body["run"]["id"] == first_body["run"]["id"]
    assert launched["calls"] == [(first_body["run"]["id"], False)]

    with database.SessionLocal() as db:
        session = db.get(Session, first_body["session"]["id"])
        run = db.get(Run, first_body["run"]["id"])
        assert session is not None
        assert session.workspace_id == DEFAULT_WORKSPACE_ID
        assert session.agent_id == DEFAULT_AGENT_ID
        assert session.model_connection_id is None
        assert session.thinking_level == "high"
        assert run is not None and run.agent_id == DEFAULT_AGENT_ID
        messages = list(db.scalars(select(ChatMessage).where(ChatMessage.session_id == session.id)))
        assert [(item.role, item.content) for item in messages] == [("user", payload["content"])]
        assert db.query(DraftLaunch).count() == 1


def test_launch_draft_rejects_reusing_key_for_different_request(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    first = test_client.post("/api/drafts/launch", json=_draft_payload(key="draft-mismatch"))
    assert first.status_code == 202, first.text
    counts_after_first = _draft_resource_counts()

    changed = test_client.post(
        "/api/drafts/launch",
        json=_draft_payload(key="draft-mismatch", title="A different destination"),
    )
    assert changed.status_code == 409
    assert "different draft payload" in changed.json()["detail"]
    assert _draft_resource_counts() == counts_after_first
    assert launched["calls"] == [(first.json()["run"]["id"], False)]


@pytest.mark.parametrize("failure_mode", ["false", "exception"])
def test_launch_draft_marks_persisted_run_failed_when_initial_scheduling_fails(
    client: tuple[TestClient, dict[str, list]],
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    test_client, launched = client
    attempts: list[tuple[str, bool]] = []

    def cannot_schedule(run_id: str, resume: bool = False) -> bool:
        attempts.append((run_id, resume))
        if failure_mode == "exception":
            raise RuntimeError("coordinator unavailable")
        return False

    monkeypatch.setattr(coordinator, "launch", cannot_schedule)
    payload = _draft_payload(key=f"draft-schedule-{failure_mode}")
    failed = test_client.post("/api/drafts/launch", json=payload)
    assert failed.status_code == 503, failed.text
    assert _draft_resource_counts() == (1, 1, 1, 1)

    with database.SessionLocal() as db:
        record = db.scalar(select(DraftLaunch).where(DraftLaunch.idempotency_key == payload["idempotency_key"]))
        assert record is not None and record.run_id is not None
        run = db.get(Run, record.run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "launch_unavailable"
        run_id = run.id

    retry = test_client.post("/api/drafts/launch", json=payload)
    assert retry.status_code == 200, retry.text
    assert retry.json()["run"]["id"] == run_id
    assert retry.json()["run"]["status"] == "failed"
    assert _draft_resource_counts() == (1, 1, 1, 1)
    assert attempts == [(run_id, False)]
    assert launched["calls"] == []


def test_launch_draft_reuses_project_for_distinct_keys_with_same_normalized_root(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, _launched = client
    project_root = tmp_path / "project-root"
    project_root.mkdir()
    first = test_client.post(
        "/api/drafts/launch",
        json=_draft_payload(key="draft-project-one", root_path=str(project_root / ".")),
    )
    second = test_client.post(
        "/api/drafts/launch",
        json=_draft_payload(key="draft-project-two", root_path=str(project_root)),
    )
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["workspace"]["id"] == second.json()["workspace"]["id"]
    assert first.json()["workspace"]["root_path"] == str(project_root.resolve())
    with database.SessionLocal() as db:
        assert db.query(Workspace).count() == 2  # default one-off workspace + selected project


def test_invalid_or_unavailable_draft_launch_leaves_no_rows(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    before = _draft_resource_counts()

    invalid = test_client.post(
        "/api/drafts/launch",
        json=_draft_payload(key="draft-invalid", title="   "),
    )
    assert invalid.status_code == 422
    assert _draft_resource_counts() == before

    unavailable = test_client.post(
        "/api/drafts/launch",
        json=_draft_payload(key="draft-unavailable", model_connection_id="not-a-connection"),
    )
    assert unavailable.status_code == 409
    assert _draft_resource_counts() == before
    assert launched["calls"] == []


def test_session_context_recalculates_migrated_messages_when_cache_is_zero(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    _workspace_id, _agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None
        session.context_summary = "legacy summary"
        session.context_tokens = 0
        db.add_all([
            ChatMessage(session_id=session_id, role="user", content="old question"),
            ChatMessage(session_id=session_id, role="assistant", content="old answer"),
        ])
        db.commit()

    response = test_client.get(f"/api/sessions/{session_id}/context")
    assert response.status_code == 200
    body = response.json()
    assert body["used_tokens"] > 0
    assert body["limit_tokens"] == 100_000
    assert body["compact_threshold_tokens"] == 90_000
    assert body["percent"] > 0
    assert body["last_compacted_at"] is None
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None and session.context_tokens == body["used_tokens"]


def test_session_context_get_compacts_migrated_history_at_threshold(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    _workspace_id, _agent_id, session_id = _seed()
    old_time = datetime.now(timezone.utc)
    with database.SessionLocal() as db:
        db.add_all([
            ChatMessage(
                session_id=session_id,
                role="user",
                content="界" * 45_100,
                created_at=old_time,
            ),
            ChatMessage(
                session_id=session_id,
                role="user",
                content="keep latest",
                created_at=old_time + timedelta(microseconds=1),
            ),
        ])
        db.commit()

    response = test_client.get(f"/api/sessions/{session_id}/context")
    assert response.status_code == 200
    body = response.json()
    assert 0 < body["used_tokens"] < body["compact_threshold_tokens"]
    assert body["last_compacted_at"] == old_time.replace(tzinfo=None).isoformat()
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None
        assert session.context_tokens == body["used_tokens"]
        assert session.context_summary
        assert session.last_compacted_at == old_time.replace(tzinfo=None)
