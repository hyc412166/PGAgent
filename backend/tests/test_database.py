from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from src.api.routes import router as resources_router
from src.api import routes as resources_api
from src.api.routes.sessions import cancel_session_task
from src.config import settings
from src.mcp.runtime import mcp_runtime_pool
from src.persistence import database
from src.persistence.database import (
    Base,
    DEFAULT_AGENT_ID,
    DEFAULT_AGENT_DESCRIPTION,
    DEFAULT_AGENT_NAME,
    DEFAULT_AGENT_SYSTEM_PROMPT,
    DEFAULT_WORKSPACE_ID,
    DEFAULT_WORKSPACE_DESCRIPTION,
    DEFAULT_WORKSPACE_NAME,
    Artifact,
    ModelConnection,
    ChatMessage,
    ConversationCompaction,
    ConversationTurn,
    DurableTask,
    PlanStep,
    Run,
    Session,
    configure_database,
    init_db,
)
from src.runs.service import coordinator


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
        "conversation_turns",
        "durable_tasks",
        "plan_steps",
        "runs",
        "draft_launches",
        "run_events",
        "approvals",
        "memories",
        "memory_jobs",
        "memory_settings",
        "memory_rollouts",
        "memory_citations",
        "memory_skills",
        "model_connections",
        "skills",
        "agent_tools",
        "agent_skills",
        "session_skills",
        "usage_records",
        "conversation_compactions",
        "artifacts",
    }.issubset(tables)
    assert "team_tasks" not in tables
    assert "agent_messages" not in tables
    assert "context_epochs" not in tables
    assert "context_checkpoints" not in tables
    assert "compaction_attempts" not in tables

    def unique_cover_count(table_name: str, column_name: str) -> int:
        constraints = sum(
            constraint.get("column_names") == [column_name]
            for constraint in inspect(database.engine).get_unique_constraints(table_name)
        )
        indexes = sum(
            bool(index.get("unique")) and index.get("column_names") == [column_name]
            for index in inspect(database.engine).get_indexes(table_name)
        )
        return constraints + indexes

    assert unique_cover_count("chat_messages", "terminal_for_turn_id") == 1
    assert unique_cover_count("runs", "turn_id") == 1
    run_columns = {column["name"] for column in inspect(database.engine).get_columns("runs")}
    assert {"task_id", "plan_step_id", "run_kind", "resumed_from_run_id"}.issubset(run_columns)
    memory_indexes = {index["name"] for index in inspect(database.engine).get_indexes("memory_rollouts")}
    citation_indexes = {index["name"] for index in inspect(database.engine).get_indexes("memory_citations")}
    skill_indexes = {index["name"] for index in inspect(database.engine).get_indexes("memory_skills")}
    assert "uq_memory_rollouts_session_sequence" in memory_indexes
    assert "uq_memory_citation_target" in citation_indexes
    assert "uq_memory_skill_workspace_name" in skill_indexes


def test_session_task_api_returns_ordered_durable_plan(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={}).json()["id"]
    with database.SessionLocal() as db:
        task = DurableTask(session_id=session_id, goal="完成持久化恢复", status="needs_recovery")
        db.add(task)
        db.flush()
        first = PlanStep(
            task_id=task.id,
            external_id="inspect",
            position=1,
            title="核验已有结果",
            status="needs_recovery",
            next_action="检查工作区实际状态",
        )
        second = PlanStep(
            task_id=task.id,
            external_id="finish",
            position=2,
            title="完成剩余实现",
            status="pending",
        )
        db.add_all([second, first])
        db.flush()
        task.active_step_id = first.id
        db.commit()

    active = client.get(f"/api/sessions/{session_id}/active-task")
    history = client.get(f"/api/sessions/{session_id}/tasks")
    assert active.status_code == 200
    assert active.json()["goal"] == "完成持久化恢复"
    assert [step["external_id"] for step in active.json()["steps"]] == ["inspect", "finish"]
    assert history.status_code == 200
    assert [item["id"] for item in history.json()] == [active.json()["id"]]

    cancelled = client.post(f"/api/sessions/{session_id}/tasks/{active.json()['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert all(step["status"] in {"completed", "cancelled"} for step in cancelled.json()["steps"])
    assert client.get(f"/api/sessions/{session_id}/active-task").json() is None


def test_cancel_session_task_rejects_a_stale_task_card(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={}).json()["id"]
    with database.SessionLocal() as db:
        stale = DurableTask(session_id=session_id, goal="stale", status="paused")
        current = DurableTask(session_id=session_id, goal="current", status="running")
        db.add_all([stale, current])
        db.commit()
        stale_id = stale.id
        current_id = current.id

    first = client.post(f"/api/sessions/{session_id}/tasks/{stale_id}/cancel")
    second = client.post(f"/api/sessions/{session_id}/tasks/{stale_id}/cancel")

    assert first.status_code == 200
    assert first.json()["id"] == stale_id
    assert second.status_code == 404
    with database.SessionLocal() as db:
        assert db.get(DurableTask, stale_id).status == "cancelled"
        assert db.get(DurableTask, current_id).status == "running"


@pytest.mark.asyncio
async def test_cancel_session_task_cancels_live_run_on_the_owning_loop(tmp_path: Path) -> None:
    configure_database(f"sqlite:///{(tmp_path / 'live-cancel.db').as_posix()}")
    init_db()
    with database.SessionLocal() as db:
        session = Session(title="Live cancellation")
        db.add(session)
        db.flush()
        task = DurableTask(session_id=session.id, goal="running", status="running")
        db.add(task)
        db.flush()
        run = Run(session_id=session.id, task_id=task.id, status="running")
        db.add(run)
        db.commit()
        session_id, task_id, run_id = session.id, task.id, run.id

        live_task = asyncio.create_task(asyncio.sleep(60))
        coordinator._tasks[run_id] = live_task
        try:
            payload = await cancel_session_task(session_id, task_id, db)
            assert payload["status"] == "cancelled"
            with pytest.raises(asyncio.CancelledError):
                await live_task
        finally:
            coordinator._tasks.pop(run_id, None)


def test_turn_schema_enforces_one_terminal_reply_per_accepted_message(tmp_path: Path) -> None:
    configure_database(f"sqlite:///{(tmp_path / 'turn-schema.db').as_posix()}")
    init_db()
    with database.SessionLocal() as db:
        session = Session(title="Delivery")
        db.add(session)
        db.flush()
        turn = ConversationTurn(session_id=session.id, client_message_id="client-1")
        db.add(turn)
        db.flush()
        user = ChatMessage(
            session_id=session.id,
            role="user",
            content="hello",
            turn_id=turn.id,
            message_kind="user_request",
            sequence=1,
        )
        run = Run(session_id=session.id, turn_id=turn.id)
        first = ChatMessage(
            session_id=session.id,
            role="assistant",
            content="done",
            turn_id=turn.id,
            message_kind="terminal",
            terminal_for_turn_id=turn.id,
            sequence=2,
        )
        db.add_all([user, run, first])
        db.commit()
        assert first.id

        db.add(ChatMessage(
            session_id=session.id,
            role="assistant",
            content="duplicate",
            turn_id=turn.id,
            message_kind="terminal",
            terminal_for_turn_id=turn.id,
            sequence=3,
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    Base.metadata.drop_all(bind=database.engine)


def test_init_removes_retired_team_collaboration_tables(tmp_path: Path) -> None:
    configure_database(f"sqlite:///{(tmp_path / 'retired-team.db').as_posix()}")
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE team_tasks (id VARCHAR(36) PRIMARY KEY, title TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE agent_messages ("
            "id VARCHAR(36) PRIMARY KEY, "
            "task_id VARCHAR(36) REFERENCES team_tasks(id) ON DELETE CASCADE"
            ")"
        )
        connection.exec_driver_sql("INSERT INTO team_tasks (id, title) VALUES ('old', 'old task')")
        connection.exec_driver_sql("INSERT INTO agent_messages (id, task_id) VALUES ('msg', 'old')")

    init_db()

    tables = set(inspect(database.engine).get_table_names())
    assert "team_tasks" not in tables
    assert "agent_messages" not in tables
    assert "delegated_tasks" in tables
    assert "background_jobs" in tables


def test_retired_team_collaboration_routes_are_absent(client: TestClient) -> None:
    assert client.get("/api/teams/tasks").status_code == 404
    assert client.post("/api/teams/tasks", json={"title": "retired"}).status_code == 404
    assert client.get("/api/teams/messages").status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/api/teams")]


def test_compaction_persistence_round_trip_and_cascade(tmp_path: Path) -> None:
    """A single compacted replacement is durable and follows its session."""

    configure_database(f"sqlite:///{(tmp_path / 'context.db').as_posix()}")
    init_db()
    with database.SessionLocal() as db:
        session = Session(title="Context persistence")
        db.add(session)
        db.flush()
        compaction = ConversationCompaction(
            session_id=session.id,
            source_sequence=4,
            active_request="ship feature",
            todo_state=[{"content": "verify", "status": "pending"}],
            summary="files were updated",
            continuation_messages=[{"role": "user", "content": "<compacted-context>state</compacted-context>"}],
            transcript_artifact={"artifact_id": "artifact_" + ("b" * 24)},
            before_tokens=100_000,
            after_tokens=10_000,
            removed_message_count=3,
        )
        artifact = Artifact(
            session_id=session.id,
            kind="tool_output",
            name="listing.json",
            storage_path="artifacts/listing.json",
            sha256="a" * 64,
            preview="two files",
        )
        db.add_all((compaction, artifact))
        db.commit()
        session_id = session.id
        compaction_id = compaction.id

    with database.SessionLocal() as db:
        restored = db.get(Session, session_id)
        assert restored is not None
        assert restored.conversation_compactions[0].id == compaction_id
        assert restored.conversation_compactions[0].after_tokens == 10_000
        assert restored.artifacts[0].sha256 == "a" * 64
        db.delete(restored)
        db.commit()
        assert db.get(ConversationCompaction, compaction_id) is None
        assert db.query(Artifact).count() == 0


def test_init_incrementally_migrates_legacy_database_and_preserves_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    settings.mcp_config_file.write_text(json.dumps({
        "mcpServers": {
            "filesystem": {"command": "npx", "args": ["server-filesystem"], "enabled": True},
            "disabled": {"command": "npx", "args": ["disabled"], "enabled": False},
        }
    }), encoding="utf-8")
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
            INSERT INTO sessions
                (id, title, workspace_id, agent_id, context_summary, status, created_at, updated_at)
            VALUES
                ('legacy-session', 'Legacy MCP session', 'legacy-workspace', NULL, '', 'active',
                 '2026-01-01 00:00:00', '2026-01-01 00:00:00');
            """
        )

    configure_database(f"sqlite:///{database_path.as_posix()}")
    init_db()
    inspector = inspect(database.engine)
    agent_columns = {column["name"] for column in inspector.get_columns("agents")}
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    assert "is_default" in agent_columns
    assert "workflow_profile_id" in agent_columns
    assert {
        "model_connection_id", "model_id", "thinking_level", "permission_mode",
        "use_memories", "mcp_server_names", "context_tokens",
    }.issubset(session_columns)
    assert "context_summary" not in session_columns
    assert "last_compacted_at" not in session_columns
    with database.SessionLocal() as db:
        assert db.get(database.Workspace, "legacy-workspace") is not None
        assert db.get(database.Session, "legacy-session").mcp_server_names == ["filesystem"]
        assert db.get(database.Workspace, DEFAULT_WORKSPACE_ID) is not None
        assert db.get(database.Agent, DEFAULT_AGENT_ID) is not None
    migration_app = FastAPI()
    migration_app.include_router(resources_router)
    with TestClient(migration_app) as migration_client:
        response = migration_client.post(
            "/api/sessions", json={"model_connection_id": "missing-after-migration"}
        )
    assert response.status_code == 409


def test_init_adds_background_waiter_column_to_the_owning_table(tmp_path: Path) -> None:
    database_path = tmp_path / "background-migration.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE background_jobs (
                id VARCHAR(36) PRIMARY KEY,
                observed_by_run_id VARCHAR(36),
                plan_step_id VARCHAR(36)
            );
            """
        )

    configure_database(f"sqlite:///{database_path.as_posix()}")
    init_db()
    inspector = inspect(database.engine)
    background_columns = {column["name"] for column in inspector.get_columns("background_jobs")}
    run_columns = {column["name"] for column in inspector.get_columns("runs")}

    assert "waiting_run_id" in background_columns
    assert "waiting_run_id" not in run_columns


def test_core_resource_crud_and_dashboard(client: TestClient, tmp_path: Path) -> None:
    workspace_response = client.post(
        "/api/workspaces",
        json={"name": "Demo", "root_path": str(tmp_path / "workspace")},
    )
    assert workspace_response.status_code == 201
    workspace = workspace_response.json()

    agent_response = client.post(
        "/api/agents",
        json={
            "name": "Builder",
            "workspace_id": workspace["id"],
            "thinking_level": "medium",
            "workflow_profile_id": "coding",
        },
    )
    assert agent_response.status_code == 201
    agent = agent_response.json()
    assert agent["workflow_profile_id"] == "coding"

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
    assert dashboard["agents"] == 1
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


def test_delete_session_purges_its_complete_conversation_history(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = client.post("/api/sessions", json={"title": "Delete me"})
    assert response.status_code == 201
    session_id = response.json()["id"]
    data_dir = tmp_path / "runtime-data"
    artifact_dir = data_dir / "artifacts" / session_id
    artifact_dir.mkdir(parents=True)
    artifact_file = artifact_dir / "artifact_deadbeefdeadbeef.bin"
    artifact_file.write_text("private archived transcript", encoding="utf-8")
    monkeypatch.setattr(resources_api, "settings", SimpleNamespace(data_dir=data_dir))

    with database.SessionLocal() as db:
        run = database.Run(session_id=session_id, status="completed")
        message = database.ChatMessage(session_id=session_id, role="user", content="hello", sequence=1)
        memory = database.Memory(scope="session", scope_id=session_id, content="temporary")
        artifact = database.Artifact(
            session_id=session_id,
            name="archived transcript",
            storage_path=str(artifact_file),
            size_bytes=artifact_file.stat().st_size,
        )
        db.add_all((run, message, memory, artifact))
        db.flush()
        event = database.RunEvent(run_id=run.id, event_type="completed")
        approval = database.Approval(run_id=run.id, tool_name="write", arguments={}, status="approved")
        usage = database.UsageRecord(
            run_id=run.id,
            session_id=session_id,
            model_id="test-model",
            provider="test",
        )
        db.add_all((event, approval, usage))
        db.commit()
        run_id = run.id

    deleted = client.delete(f"/api/sessions/{session_id}")
    assert deleted.status_code == 204
    assert not artifact_dir.exists()

    with database.SessionLocal() as db:
        assert db.get(database.Session, session_id) is None
        assert db.get(database.Run, run_id) is None
        assert db.query(database.ChatMessage).filter_by(session_id=session_id).count() == 0
        assert db.query(database.RunEvent).filter_by(run_id=run_id).count() == 0
        assert db.query(database.Approval).filter_by(run_id=run_id).count() == 0
        assert db.query(database.UsageRecord).filter_by(session_id=session_id).count() == 0
        assert db.query(database.Memory).filter_by(scope="session", scope_id=session_id).count() == 0
        assert db.query(database.Artifact).filter_by(session_id=session_id).count() == 0


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
    assert default_agent["workflow_profile_id"] == "auto"
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


def test_global_and_session_memory_preferences_persist(client: TestClient) -> None:
    initial = client.get("/api/memories/settings")
    assert initial.status_code == 200
    assert initial.json() == {"enabled": True}

    disabled = client.put("/api/memories/settings", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json() == {"enabled": False}
    assert client.get("/api/memories/settings").json() == {"enabled": False}

    created = client.post("/api/sessions", json={"use_memories": False})
    assert created.status_code == 201, created.text
    assert created.json()["use_memories"] is False
    updated = client.patch(
        f"/api/sessions/{created.json()['id']}", json={"use_memories": True}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["use_memories"] is True


def test_session_mcp_selection_persists_and_rejects_unavailable_servers(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed_sessions: list[str] = []

    async def close_session(session_id: str) -> None:
        closed_sessions.append(session_id)

    monkeypatch.setattr(mcp_runtime_pool, "close_session", close_session)
    settings.mcp_config_file.write_text(json.dumps({
        "mcpServers": {
            "filesystem": {"command": "npx", "args": ["server-filesystem"], "enabled": True},
            "disabled": {"command": "npx", "args": ["disabled"], "enabled": False},
        }
    }), encoding="utf-8")

    created = client.post("/api/sessions", json={"mcp_server_names": ["filesystem"]})
    assert created.status_code == 201, created.text
    assert created.json()["mcp_server_names"] == ["filesystem"]

    cleared = client.patch(
        f"/api/sessions/{created.json()['id']}", json={"mcp_server_names": []}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["mcp_server_names"] == []
    assert closed_sessions == [created.json()["id"]]
    assert client.patch(
        f"/api/sessions/{created.json()['id']}",
        json={"mcp_server_names": ["disabled"]},
    ).status_code == 409
    assert client.patch(
        f"/api/sessions/{created.json()['id']}",
        json={"mcp_server_names": ["missing"]},
    ).status_code == 409


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


def test_agent_model_connection_must_exist_and_be_enabled(client: TestClient) -> None:
    with database.SessionLocal() as db:
        enabled = ModelConnection(
            name="Enabled child-agent relay",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            secret_ref="test:enabled-child-agent-relay",
        )
        disabled = ModelConnection(
            name="Disabled child-agent relay",
            provider="openai_compatible",
            base_url="https://example.invalid/v1",
            secret_ref="test:disabled-child-agent-relay",
            enabled=False,
        )
        db.add_all([enabled, disabled])
        db.commit()
        enabled_id = enabled.id
        disabled_id = disabled.id

    assert client.post(
        "/api/agents", json={"name": "Missing connection", "model_connection_id": "missing-connection"}
    ).status_code == 409
    assert client.post(
        "/api/agents", json={"name": "Disabled connection", "model_connection_id": disabled_id}
    ).status_code == 409

    created = client.post(
        "/api/agents", json={"name": "Enabled connection", "model_connection_id": enabled_id}
    )
    assert created.status_code == 201, created.text
    assert created.json()["model_connection_id"] == enabled_id

    agent_id = client.post("/api/agents", json={"name": "Unbound child"}).json()["id"]
    assert client.patch(
        f"/api/agents/{agent_id}", json={"model_connection_id": "missing-connection"}
    ).status_code == 409
    assert client.patch(
        f"/api/agents/{agent_id}", json={"model_connection_id": disabled_id}
    ).status_code == 409
    updated = client.patch(f"/api/agents/{agent_id}", json={"model_connection_id": enabled_id})
    assert updated.status_code == 200, updated.text
    assert updated.json()["model_connection_id"] == enabled_id


def test_run_event_read_hides_private_snapshots_and_sanitizes_timeline_payloads(client: TestClient) -> None:
    session = client.post("/api/sessions", json={}).json()
    run = client.post("/api/runs", json={"session_id": session["id"]}).json()
    with database.SessionLocal() as db:
        db.add_all([
            database.RunEvent(
                run_id=run["id"],
                event_type="runtime_snapshot",
                payload={
                    "messages": [{"role": "user", "content": "private message"}],
                    "events": [{"arguments": {"api_key": "snapshot-secret"}}],
                    "runtime_binding": {"secret_ref": "env:secret", "skill_instructions": "# SKILL.md"},
                },
            ),
            database.RunEvent(
                run_id=run["id"],
                event_type="checkpoint",
                payload={"messages": [{"content": "other private checkpoint"}]},
            ),
            database.RunEvent(
                run_id=run["id"],
                event_type="model_step_started",
                step=1,
                payload={"step": 1, "elapsed_ms": 10, "messages": [{"content": "private"}]},
            ),
            database.RunEvent(
                run_id=run["id"],
                event_type="mcp_connecting",
                payload={"servers": ["playwright"], "command": "npx secret-command"},
            ),
            database.RunEvent(
                run_id=run["id"],
                event_type="mcp_ready",
                payload={"tool_count": 24, "failed_servers": []},
            ),
            database.RunEvent(
                run_id=run["id"],
                event_type="thought_summary",
                step=1,
                payload={"step": 1, "summary": "line one\n" + ("x" * 20_050), "complete": True},
            ),
            database.RunEvent(
                run_id=run["id"],
                event_type="tool_started",
                payload={
                    "tool_name": "read",
                    "tool_call_id": "call-1",
                    "elapsed_ms": 20,
                    "thought_duration_ms": 10,
                    "arguments": {
                        "path": "safe.txt",
                        "url": "https://username:password@example.test/docs?api_key=secret#fragment",
                        "command": {"executable": "python", "argument_count": 2},
                        "query": {"chars": 18},
                        "recursive": True,
                        "content": "private file body",
                        "api_key": "tool-secret",
                    },
                    "secret_ref": "env:tool-secret",
                },
            ),
            database.RunEvent(
                run_id=run["id"],
                event_type="tool_finished",
                payload={
                    "tool_name": "read",
                    "tool_call_id": "call-1",
                    "ok": True,
                    "changed": False,
                    "duration_ms": 5,
                    "elapsed_ms": 25,
                    "output": "private tool output",
                    "messages": [{"content": "private"}],
                },
            ),
            database.RunEvent(
                run_id=run["id"],
                event_type="run_completed",
                payload={"elapsed_ms": 30, "has_output": True, "output": "private assistant output"},
            ),
        ])
        db.commit()

    response = client.get(f"/api/runs/{run['id']}/events")
    assert response.status_code == 200, response.text
    events = response.json()
    assert {event["event_type"] for event in events} == {
        "model_step_started", "mcp_connecting", "mcp_ready", "thought_summary", "tool_started", "tool_finished", "run_completed",
    }
    events_by_type = {event["event_type"]: event for event in events}
    assert events_by_type["tool_started"]["payload"] == {
        "elapsed_ms": 20,
        "thought_duration_ms": 10,
        "tool_name": "read",
        "tool_call_id": "call-1",
        "arguments": {
            "path": "safe.txt",
            "url": "https://example.test/docs",
            "command": {"executable": "python", "argument_count": 2},
            "query": {"chars": 18},
            "recursive": True,
        },
    }
    assert events_by_type["tool_finished"]["payload"] == {
        "duration_ms": 5,
        "elapsed_ms": 25,
        "changed": False,
        "ok": True,
        "tool_name": "read",
        "tool_call_id": "call-1",
    }
    thought = events_by_type["thought_summary"]["payload"]
    assert thought["complete"] is True
    assert thought["step"] == 1
    assert "\n" in thought["summary"]
    assert len(thought["summary"]) == 20_000
    assert events_by_type["mcp_connecting"]["payload"] == {"servers": ["playwright"]}
    assert events_by_type["mcp_ready"]["payload"] == {"tool_count": 24, "failed_servers": []}
    serialized = str(events)
    for private_value in (
        "private message", "snapshot-secret", "env:secret", "# SKILL.md",
        "private file body", "tool-secret", "private tool output", "private assistant output",
    ):
        assert private_value not in serialized


def test_run_reads_include_the_real_session_and_agent_names(client: TestClient) -> None:
    session = client.post("/api/sessions", json={"title": "调用 Playwright 搜索科比"}).json()
    run = client.post("/api/runs", json={"session_id": session["id"]}).json()

    listed = client.get("/api/runs").json()
    selected = next(item for item in listed if item["id"] == run["id"])
    fetched = client.get(f"/api/runs/{run['id']}").json()

    assert selected["session_title"] == "调用 Playwright 搜索科比"
    assert selected["agent_name"] == "PGAgent 主控"
    assert fetched["session_title"] == "调用 Playwright 搜索科比"


def test_context_event_includes_a_bounded_user_message_excerpt(client: TestClient) -> None:
    session = client.post("/api/sessions", json={"title": "Long prompt"}).json()
    content = "请分析这个很长的任务：" + ("细节" * 120)
    with database.SessionLocal() as db:
        turn = ConversationTurn(session_id=session["id"], client_message_id="context-message-turn")
        db.add(turn)
        db.flush()
        message = ChatMessage(
            session_id=session["id"],
            turn_id=turn.id,
            role="user",
            content=content,
            sequence=1,
            message_kind="user_request",
        )
        run = Run(session_id=session["id"], turn_id=turn.id)
        db.add_all([message, run])
        db.flush()
        db.add(database.RunEvent(
            run_id=run.id,
            event_type="context_prepared",
            payload={"estimated_tokens": 321, "omitted_messages": 2},
        ))
        db.commit()
        run_id = run.id

    event = client.get(f"/api/runs/{run_id}/events").json()[0]
    excerpt = event["payload"]["message_excerpt"]

    assert excerpt.startswith("请分析这个很长的任务：")
    assert excerpt.endswith("…")
    assert len(excerpt) == 180
    assert event["payload"]["estimated_tokens"] == 321


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
        {"use_memories": False},
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


def test_memory_api_validates_scope_and_versions_updates(client: TestClient, tmp_path: Path) -> None:
    workspace = client.post(
        "/api/workspaces", json={"name": "Memory project", "root_path": str(tmp_path / "memory-project")}
    ).json()
    invalid = client.post(
        "/api/memories",
        json={"scope": "workspace", "scope_id": "missing-workspace", "name": "x", "content": "content"},
    )
    invalid_global = client.post(
        "/api/memories",
        json={"scope": "global", "scope_id": workspace["id"], "name": "x", "content": "content"},
    )
    assert invalid.status_code == 422
    assert invalid_global.status_code == 422

    created = client.post(
        "/api/memories",
        json={
            "scope": "workspace", "scope_id": workspace["id"], "name": "Test command",
            "content": "Use pytest.", "memory_type": "project",
        },
    ).json()
    updated = client.patch(
        f"/api/memories/{created['id']}", json={"content": "Use pytest -q."},
    )
    assert updated.status_code == 200
    replacement = updated.json()
    assert replacement["id"] != created["id"]
    with database.SessionLocal() as db:
        old = db.get(database.Memory, created["id"])
        assert old is not None and old.status == "superseded"
        assert old.superseded_by == replacement["id"]

    renamed_response = client.patch(
        f"/api/memories/{replacement['id']}", json={"name": "Renamed command"},
    )
    assert renamed_response.status_code == 200
    renamed = renamed_response.json()
    assert renamed["id"] != replacement["id"]
    with database.SessionLocal() as db:
        replaced = db.get(database.Memory, replacement["id"])
        assert replaced is not None and replaced.status == "superseded"
        assert replaced.superseded_by == renamed["id"]

    conflicting = client.post(
        "/api/memories",
        json={
            "scope": "workspace", "scope_id": workspace["id"], "name": "Existing name",
            "content": "Another active record.",
        },
    ).json()
    collision = client.patch(
        f"/api/memories/{renamed['id']}", json={"name": conflicting["name"]},
    )
    assert collision.status_code == 409

    assert client.patch(
        f"/api/memories/{created['id']}", json={"status": "active"},
    ).status_code == 409


def test_deleting_session_cascades_messages(client: TestClient) -> None:
    session = client.post("/api/sessions", json={"title": "Disposable"}).json()
    client.post(
        f"/api/sessions/{session['id']}/messages",
        json={"role": "user", "content": "hello"},
    )
    assert client.delete(f"/api/sessions/{session['id']}").status_code == 204
    assert client.get(f"/api/sessions/{session['id']}/messages").status_code == 404
