"""验证数据库初始化、增量迁移、约束、级联关系以及核心资源 API。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from src.api.routes import router as resources_router
from src.api import routes as resources_api
from src.api.routes.sessions import cancel_session_task
from src.api.schemas import RunEventRead
from src.config import settings
from src.mcp.runtime import mcp_runtime_pool
from src.observability import bind_observability_context
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
from src.persistence.run_events import append_run_event
from src.runs.service import coordinator


@pytest.fixture()
# 测试夹具：client 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def client(tmp_path: Path) -> TestClient:
    # 临时 SQLite 数据库承载真实 ORM 与路由交互；test_client 负责触发资源 API，结束后删除全部表。
    configure_database(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db()
    app = FastAPI()
    app.include_router(resources_router)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(bind=database.engine)


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_init_creates_required_tables 精确标识本用例的具体条件。
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

    # 辅助方法：unique_cover_count 实现测试替身在此调用阶段需要的最小行为。
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


def test_init_migrates_legacy_run_events_in_stable_per_run_order(tmp_path: Path) -> None:
    """旧事件在升级后必须按每个 Run 的稳定历史顺序获得序号。"""

    database_path = tmp_path / "legacy-run-events.db"
    configure_database(f"sqlite:///{database_path.as_posix()}")
    tables_except_run_events = [
        table for table in Base.metadata.sorted_tables if table.name != "run_events"
    ]
    Base.metadata.create_all(bind=database.engine, tables=tables_except_run_events)

    with database.SessionLocal() as db:
        first_run = Run(status="completed")
        second_run = Run(status="completed")
        db.add_all([first_run, second_run])
        db.commit()
        first_run_id = first_run.id
        second_run_id = second_run.id

    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE run_events (
                id VARCHAR(36) PRIMARY KEY,
                run_id VARCHAR(36) NOT NULL,
                event_type VARCHAR(80) NOT NULL,
                step INTEGER,
                payload JSON NOT NULL,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            );
            """
        )
        connection.executemany(
            "INSERT INTO run_events (id, run_id, event_type, step, payload, created_at) "
            "VALUES (?, ?, ?, NULL, '{}', ?)",
            [
                ("event-b", first_run_id, "second", "2026-01-01 00:00:01"),
                ("event-c", first_run_id, "third", "2026-01-01 00:00:02"),
                ("event-a", first_run_id, "first", "2026-01-01 00:00:01"),
                ("event-d", second_run_id, "only", "2026-01-01 00:00:03"),
            ],
        )

    init_db()

    inspector = inspect(database.engine)
    event_columns = {column["name"] for column in inspector.get_columns("run_events")}
    event_indexes = {index["name"] for index in inspector.get_indexes("run_events")}
    assert {"trace_id", "sequence"}.issubset(event_columns)
    assert "ix_run_events_run_sequence" in event_indexes

    with database.SessionLocal() as db:
        first_events = list(db.scalars(
            select(database.RunEvent)
            .where(database.RunEvent.run_id == first_run_id)
            .order_by(database.RunEvent.sequence.asc())
        ))
        second_event = db.scalar(
            select(database.RunEvent).where(database.RunEvent.run_id == second_run_id)
        )

    assert [event.id for event in first_events] == ["event-a", "event-b", "event-c"]
    assert [event.sequence for event in first_events] == [1, 2, 3]
    assert second_event is not None
    assert second_event.sequence == 1
    assert second_event.trace_id is None
    serialized = RunEventRead.model_validate(first_events[0]).model_dump()
    assert serialized["sequence"] == 1
    assert serialized["trace_id"] is None


def test_append_run_event_inherits_trace_and_leaves_commit_to_caller(tmp_path: Path) -> None:
    configure_database(f"sqlite:///{(tmp_path / 'append-transaction.db').as_posix()}")
    init_db()
    with database.SessionLocal() as setup_db:
        run = Run(status="running")
        setup_db.add(run)
        setup_db.commit()
        run_id = run.id

    with database.SessionLocal() as writer:
        with bind_observability_context(trace_id="trace-current"):
            event = append_run_event(writer, run_id=run_id, event_type="model_step_started")
        assert event.sequence == 1
        assert event.trace_id == "trace-current"
        with database.SessionLocal() as reader:
            assert reader.query(database.RunEvent).filter_by(run_id=run_id).count() == 0
        writer.rollback()

    with database.SessionLocal() as writer:
        event = append_run_event(
            writer,
            run_id=run_id,
            event_type="model_step_started",
            trace_id="trace-explicit",
        )
        writer.commit()
        assert event.sequence == 1
        assert event.trace_id == "trace-explicit"


def test_create_run_event_assigns_monotonic_run_sequence(client: TestClient) -> None:
    session = client.post("/api/sessions", json={}).json()
    run = client.post("/api/runs", json={"session_id": session["id"]}).json()

    first = client.post(
        f"/api/runs/{run['id']}/events",
        json={"event_type": "first", "payload": {}},
    )
    second = client.post(
        f"/api/runs/{run['id']}/events",
        json={"event_type": "second", "payload": {}},
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["sequence"] == 1
    assert second.json()["sequence"] == 2


def test_append_run_event_allocates_unique_order_under_concurrent_commits(tmp_path: Path) -> None:
    configure_database(f"sqlite:///{(tmp_path / 'append-concurrent.db').as_posix()}")
    init_db()
    with database.SessionLocal() as db:
        first_run = Run(status="running")
        second_run = Run(status="running")
        db.add_all([first_run, second_run])
        db.commit()
        first_run_id = first_run.id
        second_run_id = second_run.id

    count = 8
    ready = threading.Barrier(count)

    def append_one(index: int) -> int:
        with database.SessionLocal() as db:
            ready.wait(timeout=5)
            event = append_run_event(
                db,
                run_id=first_run_id,
                event_type="concurrent",
                payload={"index": index},
            )
            db.commit()
            return event.sequence

    with ThreadPoolExecutor(max_workers=count) as executor:
        sequences = list(executor.map(append_one, range(count)))

    with database.SessionLocal() as db:
        persisted = list(db.scalars(
            select(database.RunEvent)
            .where(database.RunEvent.run_id == first_run_id)
            .order_by(database.RunEvent.sequence.asc())
        ))
        second_event = append_run_event(db, run_id=second_run_id, event_type="first")
        db.commit()

    assert sorted(sequences) == list(range(1, count + 1))
    assert [event.sequence for event in persisted] == list(range(1, count + 1))
    assert second_event.sequence == 1


def test_append_run_event_releases_transaction_locks_and_isolates_runs(tmp_path: Path) -> None:
    configure_database(f"sqlite:///{(tmp_path / 'append-locks.db').as_posix()}")
    init_db()
    with database.SessionLocal() as db:
        first_run = Run(status="running")
        second_run = Run(status="running")
        db.add_all([first_run, second_run])
        db.commit()
        first_run_id = first_run.id
        second_run_id = second_run.id

    def append_and_commit(run_id: str, started: threading.Event) -> int:
        with database.SessionLocal() as db:
            started.set()
            event = append_run_event(db, run_id=run_id, event_type="worker")
            db.commit()
            return event.sequence

    with database.SessionLocal() as holder, ThreadPoolExecutor(max_workers=2) as executor:
        held = append_run_event(holder, run_id=first_run_id, event_type="rolled_back")
        assert held.sequence == 1

        same_run_started = threading.Event()
        same_run = executor.submit(append_and_commit, first_run_id, same_run_started)
        assert same_run_started.wait(timeout=2)
        with pytest.raises(FutureTimeoutError):
            same_run.result(timeout=0.2)

        other_run_started = threading.Event()
        other_run = executor.submit(append_and_commit, second_run_id, other_run_started)
        assert other_run_started.wait(timeout=2)
        assert other_run.result(timeout=2) == 1

        holder.rollback()
        assert same_run.result(timeout=2) == 1

    with database.SessionLocal() as holder, ThreadPoolExecutor(max_workers=1) as executor:
        committed = append_run_event(holder, run_id=first_run_id, event_type="committed")
        assert committed.sequence == 2
        started = threading.Event()
        after_commit = executor.submit(append_and_commit, first_run_id, started)
        assert started.wait(timeout=2)
        with pytest.raises(FutureTimeoutError):
            after_commit.result(timeout=0.2)
        holder.commit()
        assert after_commit.result(timeout=2) == 3


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_session_task_api_returns_ordered_durable_plan 精确标识本用例的具体条件。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_cancel_session_task_rejects_a_stale_task_card 精确标识本用例的具体条件。
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
# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_cancel_session_task_cancels_live_run_on_the_owning_loop 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_turn_schema_enforces_one_terminal_reply_per_accepted_message 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_init_removes_retired_team_collaboration_tables 精确标识本用例的具体条件。
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


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_retired_team_collaboration_routes_are_absent 精确标识本用例的具体条件。
def test_retired_team_collaboration_routes_are_absent(client: TestClient) -> None:
    assert client.get("/api/teams/tasks").status_code == 404
    assert client.post("/api/teams/tasks", json={"title": "retired"}).status_code == 404
    assert client.get("/api/teams/messages").status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/api/teams")]


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_compaction_persistence_round_trip_and_cascade 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_init_incrementally_migrates_legacy_database_and_preserves_rows 精确标识本用例的具体条件。
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
            INSERT INTO model_connections
                (id, name, provider, base_url, secret_ref, discovered_models,
                 manual_models, default_model, thinking_level, custom_headers,
                 capabilities, status, last_error, last_checked_at, enabled,
                 created_at, updated_at)
            VALUES
                ('legacy-connection', 'Legacy model', 'openai_compatible',
                 'https://legacy.test/v1', 'credential:legacy', '[]', '["legacy-model"]',
                 'legacy-model', 'auto', '{}', '{}', 'connected', NULL, NULL, 1,
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
    connection_columns = {column["name"] for column in inspector.get_columns("model_connections")}
    agent_columns = {column["name"] for column in inspector.get_columns("agents")}
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    assert "is_default" in agent_columns
    assert "workflow_profile_id" in agent_columns
    assert "api_protocol" in connection_columns
    assert "disabled_models" in connection_columns
    assert {
        "model_connection_id", "model_id", "thinking_level", "permission_mode",
        "use_memories", "mcp_server_names", "context_tokens",
    }.issubset(session_columns)
    assert "context_summary" not in session_columns
    assert "last_compacted_at" not in session_columns
    with database.SessionLocal() as db:
        assert db.get(database.Workspace, "legacy-workspace") is not None
        legacy_connection = db.get(database.ModelConnection, "legacy-connection")
        assert legacy_connection.api_protocol == "chat_completions"
        assert legacy_connection.disabled_models == []
        assert legacy_connection.thinking_level == "medium"
        legacy_session = db.get(database.Session, "legacy-session")
        assert legacy_session.mcp_server_names == ["filesystem"]
        assert legacy_session.thinking_level == "medium"
        assert db.get(database.Workspace, DEFAULT_WORKSPACE_ID) is not None
        assert db.get(database.Agent, DEFAULT_AGENT_ID).thinking_level == "medium"
    migration_app = FastAPI()
    migration_app.include_router(resources_router)
    with TestClient(migration_app) as migration_client:
        response = migration_client.post(
            "/api/sessions", json={"model_connection_id": "missing-after-migration"}
        )
    assert response.status_code == 409


# 测试场景：初始化会将三个用户配置表中遗留的 auto/off 全部收敛为 medium。
@pytest.mark.parametrize("legacy_level", ["auto", "off"])
def test_init_migrates_all_user_thinking_settings_to_medium(
    tmp_path: Path, legacy_level: str
) -> None:
    configure_database(f"sqlite:///{(tmp_path / f'thinking-{legacy_level}.db').as_posix()}")
    database.Base.metadata.create_all(bind=database.engine)
    with database.SessionLocal() as db:
        connection = ModelConnection(
            name=f"Legacy {legacy_level} connection",
            provider="openai_compatible",
            base_url="https://legacy.example.invalid/v1",
            secret_ref=f"test:legacy:{legacy_level}",
            thinking_level=legacy_level,
        )
        agent = database.Agent(
            name=f"Legacy {legacy_level} agent",
            thinking_level=legacy_level,
        )
        session = Session(
            title=f"Legacy {legacy_level} session",
            thinking_level=legacy_level,
        )
        db.add_all([connection, agent, session])
        db.commit()
        ids = (connection.id, agent.id, session.id)

    init_db()

    with database.SessionLocal() as db:
        assert db.get(ModelConnection, ids[0]).thinking_level == "medium"
        assert db.get(database.Agent, ids[1]).thinking_level == "medium"
        assert db.get(Session, ids[2]).thinking_level == "medium"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_init_adds_background_waiter_column_to_the_owning_table 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_core_resource_crud_and_dashboard 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_workspace_directory_only_is_named_and_deduplicated 精确标识本用例的具体条件。
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
    assert workspace["validation_runtime"] == {"kind": "local", "image": None}

    configured = client.patch(
        f"/api/workspaces/{workspace['id']}",
        json={"validation_runtime": {"kind": "docker", "image": "swebench/test:latest"}},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["validation_runtime"] == {
        "kind": "docker",
        "image": "swebench/test:latest",
    }
    rejected = client.patch(
        f"/api/workspaces/{workspace['id']}",
        json={"validation_runtime": {"kind": "docker", "image": "--privileged"}},
    )
    assert rejected.status_code == 422

    duplicate = client.post(
        "/api/workspaces",
        json={"root_path": str(root.parent / "." / root.name)},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["id"] == workspace["id"]
    assert len(client.get("/api/workspaces").json()) == 2


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_workspace_filesystem_root_uses_project_fallback_name 精确标识本用例的具体条件。
def test_workspace_filesystem_root_uses_project_fallback_name(client: TestClient, tmp_path: Path) -> None:
    root = Path(tmp_path.anchor)
    created = client.post("/api/workspaces", json={"root_path": str(root)})
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "项目"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_run_listing_supports_stable_pagination 精确标识本用例的具体条件。
def test_run_listing_supports_stable_pagination(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={}).json()["id"]
    run_ids = [
        client.post("/api/runs", json={"session_id": session_id, "status": "completed"}).json()["id"]
        for _ in range(3)
    ]

    first = client.get(
        "/api/runs", params={"session_id": session_id, "limit": 2}
    ).json()
    second = client.get(
        "/api/runs",
        params={
            "session_id": session_id,
            "limit": 2,
            "before_started_at": first[-1]["started_at"],
            "before_id": first[-1]["id"],
        },
    ).json()

    listed = [item["id"] for item in [*first, *second]]
    assert len(listed) == 3
    assert len(set(listed)) == 3
    assert set(listed) == set(run_ids)


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_delete_workspace_keeps_local_files_and_deletes_complete_session_history 精确标识本用例的具体条件。
def test_delete_workspace_keeps_local_files_and_deletes_complete_session_history(
    client: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "removable-project"
    root.mkdir()
    marker = root / "user-code.py"
    marker.write_text("print('keep me')", encoding="utf-8")
    workspace = client.post("/api/workspaces", json={"root_path": str(root)}).json()
    session = client.post(
        "/api/sessions",
        json={"title": "Keep history", "workspace_id": workspace["id"]},
    ).json()
    with database.SessionLocal() as db:
        run = database.Run(session_id=session["id"], workspace_id=workspace["id"], status="completed")
        message = database.ChatMessage(
            session_id=session["id"], role="user", content="delete with project", sequence=1
        )
        usage = database.UsageRecord(
            session_id=session["id"], model_id="test-model", provider="test"
        )
        project_memory = database.Memory(
            scope="workspace",
            scope_id=workspace["id"],
            name="Project memory",
            title="Project memory",
            content="delete with project",
        )
        db.add_all((run, message, usage, project_memory))
        db.commit()
        run_id = run.id

    deleted = client.delete(f"/api/workspaces/{workspace['id']}")

    assert deleted.status_code == 204, deleted.text
    assert marker.read_text(encoding="utf-8") == "print('keep me')"
    assert all(item["id"] != workspace["id"] for item in client.get("/api/workspaces").json())
    assert client.get(f"/api/sessions/{session['id']}").status_code == 404
    with database.SessionLocal() as db:
        assert db.get(database.Session, session["id"]) is None
        assert db.get(database.Run, run_id) is None
        assert db.query(database.ChatMessage).filter_by(session_id=session["id"]).count() == 0
        assert db.query(database.UsageRecord).filter_by(session_id=session["id"]).count() == 0
        assert db.query(database.Memory).filter_by(
            scope="workspace", scope_id=workspace["id"]
        ).count() == 0


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_delete_workspace_rejects_active_project_run 精确标识本用例的具体条件。
def test_delete_workspace_rejects_active_project_run(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "active-project"
    root.mkdir()
    workspace = client.post("/api/workspaces", json={"root_path": str(root)}).json()
    session = client.post(
        "/api/sessions",
        json={"title": "Active history", "workspace_id": workspace["id"]},
    ).json()
    with database.SessionLocal() as db:
        db.add(database.Run(
            session_id=session["id"], workspace_id=workspace["id"], status="running"
        ))
        db.commit()

    deleted = client.delete(f"/api/workspaces/{workspace['id']}")

    assert deleted.status_code == 409
    assert client.get(f"/api/workspaces/{workspace['id']}").status_code == 200
    assert client.get(f"/api/sessions/{session['id']}").status_code == 200


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_delete_session_purges_its_complete_conversation_history 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_defaults_are_seeded_protected_and_used_for_new_sessions 精确标识本用例的具体条件。
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
    assert body["thinking_level"] == "medium"
    assert body["context_tokens"] == 0


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_init_migrates_saved_agent_tools_to_canonical_surface 精确标识本用例的具体条件。
def test_init_migrates_saved_agent_tools_to_canonical_surface(client: TestClient) -> None:
    child = client.post("/api/agents", json={"name": "Legacy tools"}).json()
    with database.SessionLocal() as db:
        db.add_all([
            database.AgentTool(agent_id=child["id"], tool_id="bash"),
            database.AgentTool(agent_id=child["id"], tool_id="todowrite"),
            database.AgentTool(agent_id=child["id"], tool_id="ToolSearch"),
            database.AgentTool(agent_id=child["id"], tool_id="WebFetch"),
            database.AgentTool(agent_id=child["id"], tool_id="read_file"),
            database.AgentTool(agent_id=child["id"], tool_id="write"),
            database.AgentTool(agent_id=child["id"], tool_id="edit"),
            database.AgentTool(agent_id=child["id"], tool_id="file_info"),
            database.AgentTool(agent_id=child["id"], tool_id="background_run"),
            database.AgentTool(agent_id=child["id"], tool_id="check_background"),
        ])
        db.commit()

    init_db()

    with database.SessionLocal() as db:
        migrated = db.get(database.Agent, child["id"])
        assert migrated is not None
    assert migrated.tool_ids == [
        "apply_patch", "read", "shell", "tool_search", "update_plan", "web_open", "write_stdin",
    ]


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_message_api_exposes_responses_web_search_citations 精确标识本用例的具体条件。
def test_message_api_exposes_responses_web_search_citations(client: TestClient) -> None:
    session = client.post("/api/sessions", json={}).json()
    with database.SessionLocal() as db:
        db.add(database.ChatMessage(
            session_id=session["id"],
            role="assistant",
            content="检索完成。",
            sequence=1,
            provider_payload={"native": {"protocol": "responses", "items": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "检索完成。", "annotations": [{
                    "type": "url_citation",
                    "title": "Example News",
                    "url": "https://example.com/news",
                }]}],
            }]}},
        ))
        db.commit()

    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert messages[-1]["citations"] == [{
        "url": "https://example.com/news", "title": "Example News",
    }]


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_sessions_always_use_the_fixed_coordinator 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_seed_migrates_legacy_session_binding_without_losing_messages 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_session_model_overrides_can_be_patched 精确标识本用例的具体条件。
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


@pytest.mark.parametrize("legacy_level", ["auto", "off"])
# 测试场景：用户会话契约只允许前端提供的四档显式思考强度，拒绝历史自动与关闭值重新写入。
def test_session_api_rejects_non_ui_thinking_levels(
    client: TestClient, legacy_level: str
) -> None:
    created = client.post("/api/sessions", json={"thinking_level": legacy_level})
    assert created.status_code == 422

    session_id = client.post("/api/sessions", json={}).json()["id"]
    updated = client.patch(
        f"/api/sessions/{session_id}", json={"thinking_level": legacy_level}
    )
    assert updated.status_code == 422


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_global_and_session_memory_preferences_persist 精确标识本用例的具体条件。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_session_mcp_selection_persists_and_rejects_unavailable_servers 精确标识本用例的具体条件。
def test_session_mcp_selection_persists_and_rejects_unavailable_servers(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed_sessions: list[str] = []

    # 辅助方法：close_session 实现测试替身在此调用阶段需要的最小行为。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_session_model_connection_must_exist_and_be_enabled 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_agent_model_connection_must_exist_and_be_enabled 精确标识本用例的具体条件。
def test_agent_model_allocation_cannot_be_persisted_on_child_profile(client: TestClient) -> None:
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
    ).status_code == 422
    assert client.post(
        "/api/agents", json={"name": "Disabled connection", "model_connection_id": disabled_id}
    ).status_code == 422

    created = client.post(
        "/api/agents", json={"name": "Enabled connection", "model_connection_id": enabled_id}
    )
    assert created.status_code == 422, created.text

    agent_id = client.post("/api/agents", json={"name": "Unbound child"}).json()["id"]
    assert client.patch(
        f"/api/agents/{agent_id}", json={"model_connection_id": "missing-connection"}
    ).status_code == 422
    assert client.patch(
        f"/api/agents/{agent_id}", json={"model_connection_id": disabled_id}
    ).status_code == 422
    updated = client.patch(f"/api/agents/{agent_id}", json={"model_connection_id": enabled_id})
    assert updated.status_code == 422, updated.text
    assert client.post(
        "/api/agents", json={"name": "Fixed thinking", "thinking_level": "high"}
    ).status_code == 422


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_run_event_read_hides_private_snapshots_and_sanitizes_timeline_payloads 精确标识本用例的具体条件。
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
                    "result_summary": "来源：https://api.open-meteo.com/v1/forecast",
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
    page = response.json()
    assert page["next_before"] is None
    events = page["items"]
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
        "result_summary": "来源：https://api.open-meteo.com/v1/forecast",
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_run_reads_include_the_real_session_and_agent_names 精确标识本用例的具体条件。
def test_run_reads_include_the_real_session_and_agent_names(client: TestClient) -> None:
    session = client.post("/api/sessions", json={"title": "调用 Playwright 搜索科比"}).json()
    run = client.post("/api/runs", json={"session_id": session["id"]}).json()

    listed = client.get("/api/runs").json()
    selected = next(item for item in listed if item["id"] == run["id"])
    fetched = client.get(f"/api/runs/{run['id']}").json()

    assert selected["session_title"] == "调用 Playwright 搜索科比"
    assert selected["agent_name"] == "PGAgent 主控"
    assert fetched["session_title"] == "调用 Playwright 搜索科比"


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_context_event_includes_a_bounded_user_message_excerpt 精确标识本用例的具体条件。
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

    event = client.get(f"/api/runs/{run_id}/events").json()["items"][0]
    excerpt = event["payload"]["message_excerpt"]

    assert excerpt.startswith("请分析这个很长的任务：")
    assert excerpt.endswith("…")
    assert len(excerpt) == 180
    assert event["payload"]["estimated_tokens"] == 321


# 测试场景：验证运行事件筛选在游标分页前组合执行，并按 sequence 无重复、无遗漏地读取全部公开事件。
def test_run_event_api_composes_filters_and_pages_by_sequence(client: TestClient) -> None:
    run_id = client.post("/api/runs", json={}).json()["id"]
    with database.SessionLocal() as db:
        for event_type, step in (
            ("model_step_started", 1),
            ("model_retry", 1),
            ("tool_started", 1),
            ("runtime_snapshot", 1),
            ("model_failed", 2),
            ("model_retry", 2),
            ("run_completed", 2),
        ):
            append_run_event(
                db,
                run_id=run_id,
                event_type=event_type,
                step=step,
                payload={"attempt": step},
            )
        db.commit()

    seen_sequences: list[int] = []
    before: int | None = None
    while True:
        params: dict[str, int] = {"limit": 2}
        if before is not None:
            params["before"] = before
        response = client.get(f"/api/runs/{run_id}/events", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        page_sequences = [event["sequence"] for event in page["items"]]
        assert page_sequences == sorted(page_sequences)
        seen_sequences.extend(page_sequences)
        before = page["next_before"]
        if before is None:
            break

    assert seen_sequences == [6, 7, 3, 5, 1, 2]
    assert len(seen_sequences) == len(set(seen_sequences))

    filtered = client.get(
        f"/api/runs/{run_id}/events",
        params=[
            ("event_type", "model_retry"),
            ("event_type", "model_failed"),
            ("step", "2"),
            ("errors_only", "true"),
            ("before", "7"),
            ("limit", "2"),
        ],
    )
    assert filtered.status_code == 200, filtered.text
    assert [
        (event["event_type"], event["step"], event["sequence"])
        for event in filtered.json()["items"]
    ] == [("model_failed", 2, 5), ("model_retry", 2, 6)]
    assert filtered.json()["next_before"] is None


# 测试场景：验证事件写入响应只返回安全投影，且恢复专用 snapshot/checkpoint 不能通过公共接口创建。
def test_run_event_post_uses_safe_projection_and_rejects_private_events(client: TestClient) -> None:
    sentinel = "SUPER_SECRET_SENTINEL"
    run_id = client.post("/api/runs", json={}).json()["id"]

    response = client.post(
        f"/api/runs/{run_id}/events",
        json={
            "event_type": "tool_started",
            "trace_id": "trace-public-post",
            "step": 3,
            "payload": {
                "tool_name": "read",
                "tool_call_id": "call-public-post",
                "elapsed_ms": 12,
                "arguments": {
                    "path": "safe.txt",
                    "content": sentinel,
                    "authorization": f"Bearer {sentinel}",
                },
                "internal_error": sentinel,
                "stdout": sentinel,
                "messages": [{"content": sentinel}],
            },
        },
    )

    assert response.status_code == 201, response.text
    assert sentinel.encode() not in response.content
    assert response.json() == {
        "id": response.json()["id"],
        "run_id": run_id,
        "event_type": "tool_started",
        "trace_id": "trace-public-post",
        "sequence": 1,
        "step": 3,
        "payload": {
            "elapsed_ms": 12,
            "tool_name": "read",
            "tool_call_id": "call-public-post",
            "arguments": {"path": "safe.txt"},
        },
        "created_at": response.json()["created_at"],
    }
    with database.SessionLocal() as db:
        persisted = db.scalar(select(database.RunEvent).where(database.RunEvent.run_id == run_id))
        assert persisted is not None
        assert persisted.payload["internal_error"] == sentinel

    for event_type in ("runtime_snapshot", "checkpoint", "provider_checkpoint_created"):
        rejected = client.post(
            f"/api/runs/{run_id}/events",
            json={"event_type": event_type, "payload": {"messages": [{"content": sentinel}]}},
        )
        assert rejected.status_code == 422, rejected.text
        assert sentinel.encode() not in rejected.content

    with database.SessionLocal() as db:
        assert db.query(database.RunEvent).filter_by(run_id=run_id).count() == 1


# 测试场景：模型失败事件向前端公开稳定错误标识，但不公开供应商原始错误正文。
def test_model_failure_event_exposes_only_safe_provider_identifiers(client: TestClient) -> None:
    sentinel = "SUPER_SECRET_PROVIDER_BODY"
    run_id = client.post("/api/runs", json={}).json()["id"]

    response = client.post(
        f"/api/runs/{run_id}/events",
        json={
            "event_type": "model_failed",
            "payload": {
                "error_kind": "invalid_request",
                "error_type": "IncompleteResponse",
                "provider_error_code": "invalid_prompt",
                "provider_error_type": "invalid_request_error",
                "message": sentinel,
            },
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["payload"] == {
        "error_kind": "invalid_request",
        "error_type": "IncompleteResponse",
        "provider_error_code": "invalid_prompt",
        "provider_error_type": "invalid_request_error",
    }
    assert sentinel.encode() not in response.content


@pytest.mark.parametrize("run_status", ["received", "awaiting_approval"])
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_active_run_blocks_session_runtime_setting_changes 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_non_global_memory_requires_scope_id 精确标识本用例的具体条件。
def test_non_global_memory_requires_scope_id(client: TestClient) -> None:
    response = client.post(
        "/api/memories", json={"scope": "workspace", "content": "Missing workspace id"}
    )
    assert response.status_code == 422


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_memory_api_validates_scope_and_versions_updates 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_deleting_session_cascades_messages 精确标识本用例的具体条件。
def test_deleting_session_cascades_messages(client: TestClient) -> None:
    session = client.post("/api/sessions", json={"title": "Disposable"}).json()
    client.post(
        f"/api/sessions/{session['id']}/messages",
        json={"role": "user", "content": "hello"},
    )
    assert client.delete(f"/api/sessions/{session['id']}").status_code == 204
    assert client.get(f"/api/sessions/{session['id']}/messages").status_code == 404
