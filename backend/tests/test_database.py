from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.api.resources import router as resources_router
from app import database
from app.database import (
    Base,
    DEFAULT_AGENT_ID,
    DEFAULT_AGENT_DESCRIPTION,
    DEFAULT_AGENT_NAME,
    DEFAULT_AGENT_SYSTEM_PROMPT,
    DEFAULT_WORKSPACE_ID,
    DEFAULT_WORKSPACE_DESCRIPTION,
    DEFAULT_WORKSPACE_NAME,
    ModelConnection,
    configure_database,
    init_db,
)


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    configure_database(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db()
    app = FastAPI()
    app.include_router(resources_router)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(bind=database.engine)


def test_init_creates_required_tables(tmp_path: Path) -> None:
    configure_database(f"sqlite:///{(tmp_path / 'schema.db').as_posix()}")
    init_db()
    tables = set(inspect(database.engine).get_table_names())
    assert {
        "workspaces",
        "agents",
        "sessions",
        "chat_messages",
        "runs",
        "draft_launches",
        "run_events",
        "approvals",
        "memories",
        "model_connections",
        "team_tasks",
        "agent_messages",
        "usage_records",
    }.issubset(tables)


def test_init_incrementally_migrates_legacy_database_and_preserves_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE workspaces (
                id VARCHAR(36) PRIMARY KEY, name VARCHAR(120) NOT NULL,
                description TEXT NOT NULL DEFAULT '', root_path TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT 1, created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE model_connections (
                id VARCHAR(36) PRIMARY KEY, name VARCHAR(120) NOT NULL UNIQUE,
                provider VARCHAR(50) NOT NULL, base_url TEXT NOT NULL,
                secret_ref VARCHAR(255) NOT NULL UNIQUE, discovered_models JSON NOT NULL,
                manual_models JSON NOT NULL, default_model VARCHAR(255),
                thinking_level VARCHAR(16) NOT NULL, custom_headers JSON NOT NULL,
                capabilities JSON NOT NULL, status VARCHAR(32) NOT NULL, last_error TEXT,
                last_checked_at DATETIME, enabled BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            );
            CREATE TABLE agents (
                id VARCHAR(36) PRIMARY KEY, name VARCHAR(120) NOT NULL,
                description TEXT NOT NULL DEFAULT '', system_prompt TEXT NOT NULL,
                workspace_id VARCHAR(36), model_connection_id VARCHAR(36),
                model_id VARCHAR(255), thinking_level VARCHAR(16) NOT NULL,
                mode VARCHAR(16) NOT NULL, enabled BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            );
            CREATE TABLE sessions (
                id VARCHAR(36) PRIMARY KEY, title VARCHAR(200) NOT NULL,
                workspace_id VARCHAR(36), agent_id VARCHAR(36),
                context_summary TEXT NOT NULL, status VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            );
            INSERT INTO workspaces
                (id, name, description, root_path, enabled, created_at, updated_at)
            VALUES
                ('legacy-workspace', 'Keep me', '', 'C:/legacy', 1,
                 '2026-01-01 00:00:00', '2026-01-01 00:00:00');
            """
        )

    configure_database(f"sqlite:///{database_path.as_posix()}")
    init_db()
    inspector = inspect(database.engine)
    agent_columns = {column["name"] for column in inspector.get_columns("agents")}
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    assert "is_default" in agent_columns
    assert {
        "model_connection_id", "model_id", "thinking_level", "context_tokens", "last_compacted_at"
    }.issubset(session_columns)
    with database.SessionLocal() as db:
        assert db.get(database.Workspace, "legacy-workspace") is not None
        assert db.get(database.Workspace, DEFAULT_WORKSPACE_ID) is not None
        assert db.get(database.Agent, DEFAULT_AGENT_ID) is not None
    migration_app = FastAPI()
    migration_app.include_router(resources_router)
    with TestClient(migration_app) as migration_client:
        response = migration_client.post(
            "/api/sessions", json={"model_connection_id": "missing-after-migration"}
        )
    assert response.status_code == 409


def test_core_resource_crud_and_dashboard(client: TestClient, tmp_path: Path) -> None:
    workspace_response = client.post(
        "/api/workspaces",
        json={"name": "Demo", "root_path": str(tmp_path / "workspace")},
    )
    assert workspace_response.status_code == 201
    workspace = workspace_response.json()

    agent_response = client.post(
        "/api/agents",
        json={"name": "Builder", "workspace_id": workspace["id"], "thinking_level": "medium"},
    )
    assert agent_response.status_code == 201
    agent = agent_response.json()

    session_response = client.post(
        "/api/sessions",
        json={"title": "Build task", "workspace_id": workspace["id"], "agent_id": agent["id"]},
    )
    assert session_response.status_code == 201
    session = session_response.json()

    message_response = client.post(
        f"/api/sessions/{session['id']}/messages",
        json={"role": "user", "content": "Create an app", "metadata": {"source": "test"}},
    )
    assert message_response.status_code == 201
    assert message_response.json()["metadata"] == {"source": "test"}

    run_response = client.post(
        "/api/runs",
        json={
            "session_id": session["id"],
            "workspace_id": workspace["id"],
            "agent_id": agent["id"],
        },
    )
    assert run_response.status_code == 201
    run = run_response.json()
    with database.SessionLocal() as db:
        approval = database.Approval(
            run_id=run["id"], tool_name="write_file", arguments={"path": "a.txt"}
        )
        db.add(approval)
        db.commit()
        approval_id = approval.id
    approvals = client.get(f"/api/approvals?run_id={run['id']}&status=pending")
    assert approvals.status_code == 200
    assert [item["id"] for item in approvals.json()] == [approval_id]
    assert client.post(
        "/api/approvals",
        json={"run_id": run["id"], "tool_name": "write_file", "arguments": {}},
    ).status_code == 405
    assert client.patch(
        f"/api/approvals/{approval_id}", json={"status": "approved"}
    ).status_code == 404

    memory = client.post(
        "/api/memories",
        json={
            "scope": "workspace",
            "scope_id": workspace["id"],
            "content": "Prefer Python",
            "pinned": True,
            "metadata": {"kind": "preference"},
        },
    )
    assert memory.status_code == 201
    assert memory.json()["metadata"] == {"kind": "preference"}

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["workspaces"] == 2
    assert dashboard["agents"] == 2
    assert dashboard["sessions"] == 1
    assert dashboard["active_runs"] == 1
    assert dashboard["pending_approvals"] == 1


def test_workspace_directory_only_is_named_and_deduplicated(
    client: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "selected-project"
    root.mkdir()

    created = client.post("/api/workspaces", json={"root_path": str(root)})
    assert created.status_code == 201, created.text
    workspace = created.json()
    assert workspace["name"] == "selected-project"
    assert workspace["description"] == ""
    assert Path(workspace["root_path"]).resolve() == root.resolve()

    duplicate = client.post(
        "/api/workspaces",
        json={"root_path": str(root.parent / "." / root.name)},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["id"] == workspace["id"]
    assert len(client.get("/api/workspaces").json()) == 2


def test_workspace_filesystem_root_uses_project_fallback_name(client: TestClient, tmp_path: Path) -> None:
    root = Path(tmp_path.anchor)
    created = client.post("/api/workspaces", json={"root_path": str(root)})
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "项目"


def test_defaults_are_seeded_protected_and_used_for_new_sessions(client: TestClient) -> None:
    workspaces = client.get("/api/workspaces").json()
    agents = client.get("/api/agents").json()
    default_workspace = next(item for item in workspaces if item["id"] == DEFAULT_WORKSPACE_ID)
    default_agent = next(item for item in agents if item["id"] == DEFAULT_AGENT_ID)
    assert Path(default_workspace["root_path"]).resolve() == (
        database.PROJECT_ROOT / "data" / "workspaces" / "default"
    ).resolve()
    assert default_agent["is_default"] is True
    assert default_workspace["name"] == DEFAULT_WORKSPACE_NAME
    assert default_workspace["description"] == DEFAULT_WORKSPACE_DESCRIPTION
    assert default_agent["name"] == DEFAULT_AGENT_NAME
    assert default_agent["description"] == DEFAULT_AGENT_DESCRIPTION
    assert default_agent["system_prompt"] == DEFAULT_AGENT_SYSTEM_PROMPT
    assert default_agent["mode"] == "auto"
    assert client.delete(f"/api/workspaces/{DEFAULT_WORKSPACE_ID}").status_code == 409
    assert client.delete(f"/api/agents/{DEFAULT_AGENT_ID}").status_code == 409
    for payload in (
        {"system_prompt": "override default behavior"},
        {"description": "Updated description"},
        {"name": "Renamed master"},
        {"enabled": False},
    ):
        rejected = client.patch(f"/api/agents/{DEFAULT_AGENT_ID}", json=payload)
        assert rejected.status_code == 409, rejected.text

    with database.SessionLocal() as db:
        default_agent_row = db.get(database.Agent, DEFAULT_AGENT_ID)
        assert default_agent_row is not None
        default_agent_row.name = "legacy renamed coordinator"
        default_agent_row.description = "legacy description"
        default_agent_row.system_prompt = "legacy override"
        default_agent_row.workspace_id = None
        default_agent_row.enabled = False
        db.commit()
    init_db()
    repaired_default = client.get(f"/api/agents/{DEFAULT_AGENT_ID}")
    assert repaired_default.status_code == 200
    assert repaired_default.json()["name"] == DEFAULT_AGENT_NAME
    assert repaired_default.json()["description"] == DEFAULT_AGENT_DESCRIPTION
    assert repaired_default.json()["system_prompt"] == DEFAULT_AGENT_SYSTEM_PROMPT
    assert repaired_default.json()["workspace_id"] == DEFAULT_WORKSPACE_ID
    assert repaired_default.json()["enabled"] is True

    created = client.post("/api/sessions", json={})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["workspace_id"] == DEFAULT_WORKSPACE_ID
    assert body["agent_id"] == DEFAULT_AGENT_ID
    assert body["thinking_level"] == "auto"
    assert body["context_tokens"] == 0
    assert body["last_compacted_at"] is None


def test_sessions_always_use_the_fixed_coordinator(client: TestClient) -> None:
    child = client.post("/api/agents", json={"name": "Specialist child"}).json()

    created = client.post(
        "/api/sessions",
        json={"title": "Use a child", "agent_id": child["id"]},
    )
    assert created.status_code == 201, created.text
    session = created.json()
    assert session["agent_id"] == DEFAULT_AGENT_ID

    patched = client.patch(f"/api/sessions/{session['id']}", json={"agent_id": child["id"]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["agent_id"] == DEFAULT_AGENT_ID

    run = client.post(
        "/api/runs",
        json={"session_id": session["id"], "agent_id": child["id"]},
    )
    assert run.status_code == 201, run.text
    assert run.json()["agent_id"] == DEFAULT_AGENT_ID


def test_seed_migrates_legacy_session_binding_without_losing_messages(client: TestClient) -> None:
    with database.SessionLocal() as db:
        child = database.Agent(name="Legacy child")
        db.add(child)
        db.flush()
        session = database.Session(title="Legacy conversation", agent_id=child.id)
        db.add(session)
        db.flush()
        db.add(database.ChatMessage(session_id=session.id, role="user", content="keep this message"))
        db.commit()
        session_id = session.id

    init_db()

    with database.SessionLocal() as db:
        session = db.get(database.Session, session_id)
        assert session is not None
        assert session.agent_id == DEFAULT_AGENT_ID
        messages = db.query(database.ChatMessage).filter_by(session_id=session_id).all()
        assert [message.content for message in messages] == ["keep this message"]


def test_session_model_overrides_can_be_patched(client: TestClient) -> None:
    with database.SessionLocal() as db:
        connection = ModelConnection(
            name="Session relay",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            secret_ref="test:session-relay",
        )
        db.add(connection)
        db.commit()
        connection_id = connection.id
    session = client.post("/api/sessions", json={}).json()
    updated = client.patch(
        f"/api/sessions/{session['id']}",
        json={
            "model_connection_id": connection_id,
            "model_id": "provider/model-a",
            "thinking_level": "high",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["model_connection_id"] == connection_id
    assert updated.json()["model_id"] == "provider/model-a"
    assert updated.json()["thinking_level"] == "high"


def test_session_model_connection_must_exist_and_be_enabled(client: TestClient) -> None:
    with database.SessionLocal() as db:
        disabled = ModelConnection(
            name="Disabled relay",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            secret_ref="test:disabled-session-relay",
            enabled=False,
        )
        db.add(disabled)
        db.commit()
        disabled_id = disabled.id

    assert client.post(
        "/api/sessions", json={"model_connection_id": "missing-connection"}
    ).status_code == 409
    assert client.post(
        "/api/sessions", json={"model_connection_id": disabled_id}
    ).status_code == 409

    session_id = client.post("/api/sessions", json={}).json()["id"]
    assert client.patch(
        f"/api/sessions/{session_id}", json={"model_connection_id": "missing-connection"}
    ).status_code == 409
    assert client.patch(
        f"/api/sessions/{session_id}", json={"model_connection_id": disabled_id}
    ).status_code == 409


@pytest.mark.parametrize("run_status", ["received", "awaiting_approval"])
def test_active_run_blocks_session_runtime_setting_changes(
    client: TestClient, run_status: str
) -> None:
    with database.SessionLocal() as db:
        connection = ModelConnection(
            name=f"Lock relay {run_status}",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            secret_ref=f"test:lock-relay:{run_status}",
            enabled=True,
        )
        db.add(connection)
        db.commit()
        connection_id = connection.id

    session_id = client.post("/api/sessions", json={}).json()["id"]
    run = client.post(
        "/api/runs", json={"session_id": session_id, "status": run_status}
    )
    assert run.status_code == 201
    for payload in (
        {"model_connection_id": connection_id},
        {"model_id": "provider/changed-model"},
        {"thinking_level": "high"},
    ):
        response = client.patch(f"/api/sessions/{session_id}", json=payload)
        assert response.status_code == 409, (payload, response.text)

    metadata_only = client.patch(
        f"/api/sessions/{session_id}", json={"title": "Rename while running"}
    )
    assert metadata_only.status_code == 200
    assert metadata_only.json()["title"] == "Rename while running"


def test_non_global_memory_requires_scope_id(client: TestClient) -> None:
    response = client.post(
        "/api/memories", json={"scope": "workspace", "content": "Missing workspace id"}
    )
    assert response.status_code == 422


def test_deleting_session_cascades_messages(client: TestClient) -> None:
    session = client.post("/api/sessions", json={"title": "Disposable"}).json()
    client.post(
        f"/api/sessions/{session['id']}/messages",
        json={"role": "user", "content": "hello"},
    )
    assert client.delete(f"/api/sessions/{session['id']}").status_code == 204
    assert client.get(f"/api/sessions/{session['id']}/messages").status_code == 404


def test_team_task_claim_uses_version_and_lease(client: TestClient) -> None:
    agent = client.post("/api/agents", json={"name": "Worker"}).json()
    task_response = client.post(
        "/api/teams/tasks",
        json={"title": "Implement worker", "priority": "high", "idempotency_key": "task-once"},
    )
    assert task_response.status_code == 201
    task = task_response.json()
    assert task["status"] == "todo"
    duplicate = client.post(
        "/api/teams/tasks",
        json={"title": "Ignored duplicate body", "idempotency_key": "task-once"},
    )
    assert duplicate.json()["id"] == task["id"]

    claimed = client.post(
        f"/api/teams/tasks/{task['id']}/claim",
        json={
            "agent_id": agent["id"],
            "lease_owner": "process-1",
            "expected_version": task["version"],
            "lease_seconds": 60,
        },
    )
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "in_progress"
    assert claimed.json()["version"] == task["version"] + 1

    stale_claim = client.post(
        f"/api/teams/tasks/{task['id']}/claim",
        json={
            "agent_id": agent["id"],
            "lease_owner": "process-2",
            "expected_version": task["version"],
        },
    )
    assert stale_claim.status_code == 409

    stale_update = client.patch(
        f"/api/teams/tasks/{task['id']}",
        json={"status": "review", "expected_version": task["version"]},
    )
    assert stale_update.status_code == 409


def test_agent_messages_are_idempotent_and_acknowledgeable(client: TestClient) -> None:
    payload = {
        "message_type": "PROGRESS",
        "payload": {"percent": 50},
        "idempotency_key": "progress-once",
    }
    first = client.post("/api/teams/messages", json=payload)
    second = client.post("/api/teams/messages", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    delivered = client.post(f"/api/teams/messages/{first.json()['id']}/deliver")
    assert delivered.status_code == 200
    assert delivered.json()["status"] == "delivered"
    ack = client.post(f"/api/teams/messages/{first.json()['id']}/ack")
    assert ack.status_code == 200
    assert ack.json()["status"] == "acknowledged"
