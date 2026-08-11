from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import database
from app.api.runtime import router
from app.database import (
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    Agent,
    Approval,
    Base,
    ChatMessage,
    Run,
    RunEvent,
    Session,
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
