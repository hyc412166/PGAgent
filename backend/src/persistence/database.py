"""Database engine, migrations, seed data, and request-scoped sessions.

ORM entity declarations live in :mod:`src.persistence.models`; this module
owns engine configuration, SQLite migrations, default seeding, and the public
persistence imports used by application code.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, delete, event, inspect, or_, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession, sessionmaker

from src.tools.catalog import BUILTIN_TOOL_IDS
from src.persistence.base import Base, TimestampMixin, new_id, utcnow
from src.persistence.defaults import (
    DEFAULT_AGENT_DESCRIPTION,
    DEFAULT_AGENT_ID,
    DEFAULT_AGENT_NAME,
    DEFAULT_AGENT_SYSTEM_PROMPT,
    DEFAULT_DATABASE_PATH,
    DEFAULT_DATABASE_URL,
    DEFAULT_WORKSPACE_DESCRIPTION,
    DEFAULT_WORKSPACE_ID,
    DEFAULT_WORKSPACE_NAME,
    PROJECT_ROOT,
)
from src.persistence.models import (
    Agent,
    AgentSkill,
    AgentTool,
    Approval,
    Artifact,
    BackgroundJob,
    ChatMessage,
    CollaborationEvent,
    CollaborationMessage,
    CollaborationTeam,
    ConversationCompaction,
    ConversationTurn,
    DelegatedTask,
    DraftLaunch,
    DurableTask,
    Memory,
    MemoryCitation,
    MemoryJob,
    MemoryRollout,
    MemorySettings,
    MemorySkill,
    ModelConnection,
    PlanStep,
    PlanStepDependency,
    Run,
    RunEvent,
    Session,
    SessionSkill,
    Skill,
    TeammateWorker,
    UsageRecord,
    Workspace,
    next_chat_message_sequence,
)


def _database_url() -> str:
    return os.getenv("PGAGENT_DATABASE_URL", DEFAULT_DATABASE_URL)


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        database_file = url.removeprefix("sqlite:///")
        if database_file and database_file != ":memory:":
            Path(database_file).parent.mkdir(parents=True, exist_ok=True)
    result = create_engine(
        url,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        future=True,
    )
    if url.startswith("sqlite"):
        @event.listens_for(result, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return result


engine = _make_engine(_database_url())
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def configure_database(url: str) -> None:
    """Rebind persistence, primarily for isolated tests and local tooling."""
    global engine, SessionLocal
    engine.dispose()
    engine = _make_engine(url)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


_SQLITE_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "workspaces": {
        "validation_runtime": "JSON NOT NULL DEFAULT '{}'",
    },
    "model_connections": {
        "api_protocol": "VARCHAR(32) NOT NULL DEFAULT 'chat_completions'",
    },
    "agents": {
        "is_default": "BOOLEAN NOT NULL DEFAULT 0",
        "workflow_profile_id": "VARCHAR(16) NOT NULL DEFAULT 'auto'",
    },
    "sessions": {
        "model_connection_id": "VARCHAR(36)",
        "model_id": "VARCHAR(255)",
        "thinking_level": "VARCHAR(16) NOT NULL DEFAULT 'auto'",
        "permission_mode": "VARCHAR(16) NOT NULL DEFAULT 'smart'",
        "use_memories": "BOOLEAN NOT NULL DEFAULT 1",
        "mcp_server_names": "JSON NOT NULL DEFAULT '[]'",
        "context_tokens": "INTEGER NOT NULL DEFAULT 0",
    },
    "chat_messages": {
        "sequence": "INTEGER NOT NULL DEFAULT 0",
        "turn_id": "VARCHAR(36)",
        "message_kind": "VARCHAR(24) NOT NULL DEFAULT 'transcript'",
        "terminal_for_turn_id": "VARCHAR(36)",
        "provider_payload": "JSON NOT NULL DEFAULT '{}'",
    },
    "runs": {
        "turn_id": "VARCHAR(36)",
        "task_id": "VARCHAR(36)",
        "plan_step_id": "VARCHAR(36)",
        "run_kind": "VARCHAR(24) NOT NULL DEFAULT 'initial'",
        "resumed_from_run_id": "VARCHAR(36)",
    },
    "memories": {
        "name": "VARCHAR(200) NOT NULL DEFAULT 'Memory'",
        "memory_type": "VARCHAR(24) NOT NULL DEFAULT 'project'",
        "description": "TEXT NOT NULL DEFAULT ''",
        "tags": "JSON NOT NULL DEFAULT '[]'",
        "status": "VARCHAR(24) NOT NULL DEFAULT 'active'",
        "source_session_id": "VARCHAR(36)",
        "source_turn_id": "VARCHAR(36)",
        "superseded_by": "VARCHAR(36)",
        "usage_count": "INTEGER NOT NULL DEFAULT 0",
        "last_usage_at": "DATETIME",
        "consolidated_at": "DATETIME",
    },
    "memory_jobs": {
        "lease_expires_at": "DATETIME",
    },
    "memory_rollouts": {
        "consolidation_job_id": "VARCHAR(36)",
    },
    "background_jobs": {
        "observed_by_run_id": "VARCHAR(36)",
        "plan_step_id": "VARCHAR(36)",
        "waiting_run_id": "VARCHAR(36)",
    },
    "plan_steps": {
        "executor_kind": "VARCHAR(24) NOT NULL DEFAULT 'main'",
        "assigned_agent_id": "VARCHAR(36)",
        "assigned_run_id": "VARCHAR(36)",
        "claim_owner": "VARCHAR(160)",
        "workspace_mode": "VARCHAR(24) NOT NULL DEFAULT 'shared'",
        "worktree_path": "TEXT",
        "attempt": "INTEGER NOT NULL DEFAULT 0",
        "error": "TEXT",
    },
    "delegated_tasks": {
        "plan_step_id": "VARCHAR(36)",
        "teammate_id": "VARCHAR(36)",
    },
}


def _migrate_sqlite_columns() -> None:
    """Add new columns to existing SQLite tables without rebuilding user data."""

    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table_name, additions in _SQLITE_COLUMN_MIGRATIONS.items():
            if table_name not in tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, declaration in additions.items():
                if column_name not in existing:
                    connection.exec_driver_sql(
                        f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {declaration}'
                    )
            if table_name == "chat_messages" and "sequence" not in existing:
                # Backfill a deterministic per-session cursor for old rows.
                # SQLite's row order is not a durable ordering guarantee, so
                # use the existing display timestamp plus the UUID tie-breaker
                # exactly once during migration; all new writes use the cursor.
                rows = connection.exec_driver_sql(
                    'SELECT id, session_id FROM chat_messages ORDER BY session_id, created_at, id'
                ).fetchall()
                counters: dict[str, int] = {}
                for row_id, session_id in rows:
                    key = str(session_id)
                    counters[key] = counters.get(key, 0) + 1
                    connection.exec_driver_sql(
                        'UPDATE chat_messages SET sequence = ? WHERE id = ?',
                        (counters[key], row_id),
                    )
            if table_name == "sessions" and "mcp_server_names" not in existing:
                # Before per-session selection existed every conversation used
                # every enabled MCP server. Preserve that behavior for historic
                # rows while newly created sessions explicitly default to none.
                from src.config import settings
                from src.mcp.config import load_mcp_config_source

                config = load_mcp_config_source(settings.mcp_config_file)
                enabled_names = sorted(
                    name for name, server in config.servers.items() if server.enabled
                )
                connection.exec_driver_sql(
                    "UPDATE sessions SET mcp_server_names = ?",
                    (json.dumps(enabled_names, ensure_ascii=False),),
                )
            if table_name == "memories" and "name" not in existing:
                connection.exec_driver_sql(
                    "UPDATE memories SET name = title WHERE title IS NOT NULL AND title != ''"
                )


def _migrate_sqlite_indexes() -> None:
    """Add exactly-once indexes that SQLite cannot gain through ADD COLUMN."""

    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    def has_unique_constraint(table_name: str, column_name: str) -> bool:
        return any(
            constraint.get("column_names") == [column_name]
            for constraint in inspector.get_unique_constraints(table_name)
        )

    def has_unique_index(table_name: str, column_name: str) -> bool:
        return any(
            bool(index.get("unique")) and index.get("column_names") == [column_name]
            for index in inspector.get_indexes(table_name)
        )

    with engine.begin() as connection:
        if "memory_rollouts" in tables:
            connection.exec_driver_sql(
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_memory_rollouts_session_sequence" '
                'ON "memory_rollouts" ("source_session_id", "source_end_sequence")'
            )
        if "memory_citations" in tables:
            connection.exec_driver_sql(
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_memory_citation_target" '
                'ON "memory_citations" ("run_id", "target_type", "target_id")'
            )
        if "memory_skills" in tables:
            connection.exec_driver_sql(
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_memory_skill_workspace_name" '
                'ON "memory_skills" ("workspace_id", "name")'
            )
        if "chat_messages" in tables:
            connection.exec_driver_sql(
                'CREATE INDEX IF NOT EXISTS "ix_chat_messages_turn_id" ON "chat_messages" ("turn_id")'
            )
            if has_unique_constraint("chat_messages", "terminal_for_turn_id"):
                connection.exec_driver_sql('DROP INDEX IF EXISTS "uq_chat_messages_terminal_turn"')
            elif not has_unique_index("chat_messages", "terminal_for_turn_id"):
                connection.exec_driver_sql(
                    'CREATE UNIQUE INDEX "uq_chat_messages_terminal_turn" '
                    'ON "chat_messages" ("terminal_for_turn_id") '
                    'WHERE "terminal_for_turn_id" IS NOT NULL'
                )
        if "runs" in tables:
            run_columns = {column["name"] for column in inspector.get_columns("runs")}
            for column_name in ("task_id", "plan_step_id", "resumed_from_run_id"):
                if column_name in run_columns:
                    connection.exec_driver_sql(
                        f'CREATE INDEX IF NOT EXISTS "ix_runs_{column_name}" ON "runs" ("{column_name}")'
                    )
            if has_unique_constraint("runs", "turn_id"):
                connection.exec_driver_sql('DROP INDEX IF EXISTS "uq_runs_turn_id"')
            elif not has_unique_index("runs", "turn_id"):
                connection.exec_driver_sql(
                    'CREATE UNIQUE INDEX "uq_runs_turn_id" '
                    'ON "runs" ("turn_id") WHERE "turn_id" IS NOT NULL'
                )
        if "plan_steps" in tables:
            plan_step_columns = {column["name"] for column in inspector.get_columns("plan_steps")}
            for column_name in ("executor_kind", "assigned_agent_id", "assigned_run_id"):
                if column_name in plan_step_columns:
                    connection.exec_driver_sql(
                        f'CREATE INDEX IF NOT EXISTS "ix_plan_steps_{column_name}" '
                        f'ON "plan_steps" ("{column_name}")'
                    )
        if "delegated_tasks" in tables:
            delegated_columns = {column["name"] for column in inspector.get_columns("delegated_tasks")}
            for column_name in ("plan_step_id", "teammate_id"):
                if column_name in delegated_columns:
                    connection.exec_driver_sql(
                        f'CREATE INDEX IF NOT EXISTS "ix_delegated_tasks_{column_name}" '
                        f'ON "delegated_tasks" ("{column_name}")'
                    )
        if "background_jobs" in tables:
            background_columns = {column["name"] for column in inspector.get_columns("background_jobs")}
            for column_name in ("plan_step_id", "waiting_run_id"):
                if column_name in background_columns:
                    connection.exec_driver_sql(
                        f'CREATE INDEX IF NOT EXISTS "ix_background_jobs_{column_name}" '
                        f'ON "background_jobs" ("{column_name}")'
                    )
        if "memories" in tables:
            for column_name in (
                "name", "memory_type", "status", "source_session_id", "source_turn_id", "superseded_by",
            ):
                connection.exec_driver_sql(
                    f'CREATE INDEX IF NOT EXISTS "ix_memories_{column_name}" '
                    f'ON "memories" ("{column_name}")'
                )
            duplicate_groups = connection.exec_driver_sql(
                "SELECT scope, COALESCE(scope_id, ''), LOWER(name) "
                "FROM memories WHERE status = 'active' "
                "GROUP BY scope, COALESCE(scope_id, ''), LOWER(name) HAVING COUNT(*) > 1"
            ).fetchall()
            for scope, normalized_scope_id, normalized_name in duplicate_groups:
                if normalized_scope_id:
                    rows = connection.exec_driver_sql(
                        "SELECT id FROM memories WHERE status = 'active' AND scope = ? AND scope_id = ? "
                        "AND LOWER(name) = ? ORDER BY updated_at DESC, created_at DESC, id DESC",
                        (scope, normalized_scope_id, normalized_name),
                    ).fetchall()
                else:
                    rows = connection.exec_driver_sql(
                        "SELECT id FROM memories WHERE status = 'active' AND scope = ? AND scope_id IS NULL "
                        "AND LOWER(name) = ? ORDER BY updated_at DESC, created_at DESC, id DESC",
                        (scope, normalized_name),
                    ).fetchall()
                winner = str(rows[0][0])
                for row in rows[1:]:
                    connection.exec_driver_sql(
                        "UPDATE memories SET status = 'superseded', superseded_by = ? WHERE id = ?",
                        (winner, str(row[0])),
                    )
            connection.exec_driver_sql(
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_memories_active_scope_name" '
                'ON "memories" ("scope", COALESCE("scope_id", \'\'), LOWER("name")) '
                'WHERE "status" = \'active\''
            )
        if "memory_jobs" in tables:
            connection.exec_driver_sql(
                'CREATE INDEX IF NOT EXISTS "ix_memory_jobs_lease_expires_at" '
                'ON "memory_jobs" ("lease_expires_at")'
            )
            connection.exec_driver_sql(
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_memory_jobs_run_kind" '
                'ON "memory_jobs" ("run_id", "kind") WHERE "run_id" IS NOT NULL'
            )


def _drop_retired_team_collaboration_tables() -> None:
    """Remove the retired multi-user task-board storage from existing databases."""

    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        # Messages reference tasks, so the dependent table must be removed first.
        connection.exec_driver_sql('DROP TABLE IF EXISTS "agent_messages"')
        connection.exec_driver_sql('DROP TABLE IF EXISTS "team_tasks"')


def _migrate_retired_context_storage() -> None:
    """Detach artifacts from legacy epochs, then remove the old pipeline.

    SQLite cannot drop a foreign-key column in place.  Existing installations
    therefore need one table rebuild before ``context_epochs`` can disappear;
    otherwise deleting a session later fails while checking the dangling FK.
    """

    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    artifact_columns = {
        str(column.get("name"))
        for column in inspector.get_columns("artifacts")
    } if "artifacts" in tables else set()
    if "epoch_id" in artifact_columns:
        old_indexes = [
            str(item.get("name"))
            for item in inspector.get_indexes("artifacts")
            if item.get("name")
        ]
        with engine.begin() as connection:
            connection.exec_driver_sql('ALTER TABLE "artifacts" RENAME TO "artifacts_legacy_context"')
            for index_name in old_indexes:
                escaped = index_name.replace('"', '""')
                connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{escaped}"')
            Artifact.__table__.create(bind=connection, checkfirst=False)
            columns = (
                "id", "session_id", "kind", "name", "storage_path", "sha256",
                "mime_type", "size_bytes", "preview", "source_event_id", "metadata",
                "status", "expires_at", "created_at", "updated_at",
            )
            names = ", ".join(f'"{name}"' for name in columns)
            connection.exec_driver_sql(
                f'INSERT INTO "artifacts" ({names}) '
                f'SELECT {names} FROM "artifacts_legacy_context"'
            )
            connection.exec_driver_sql('DROP TABLE "artifacts_legacy_context"')
    with engine.begin() as connection:
        connection.exec_driver_sql('DROP TABLE IF EXISTS "compaction_attempts"')
        connection.exec_driver_sql('DROP TABLE IF EXISTS "context_checkpoints"')
        connection.exec_driver_sql('DROP TABLE IF EXISTS "context_epochs"')


def _drop_retired_session_context_columns() -> None:
    """Remove obsolete NOT NULL columns that would reject new conversations."""

    if engine.dialect.name != "sqlite":
        return
    columns = {
        str(column.get("name"))
        for column in inspect(engine).get_columns("sessions")
    }
    with engine.begin() as connection:
        for name in ("context_summary", "last_compacted_at"):
            if name in columns:
                connection.exec_driver_sql(f'ALTER TABLE "sessions" DROP COLUMN "{name}"')


def _default_workspace_root() -> Path:
    return PROJECT_ROOT / "data" / "workspaces" / "default"


def _seed_defaults() -> None:
    root = _default_workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        workspace = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        if workspace is None:
            workspace = Workspace(
                id=DEFAULT_WORKSPACE_ID,
                name="Default Workspace",
                description="PGAgent default local workspace",
                root_path=str(root),
                enabled=True,
            )
            db.add(workspace)
            db.flush()
        # The built-in workspace holds one-off tasks that have no project
        # directory. Its identity and filesystem root stay stable on upgrade.
        workspace.name = DEFAULT_WORKSPACE_NAME
        workspace.description = DEFAULT_WORKSPACE_DESCRIPTION
        workspace.root_path = str(root)
        workspace.enabled = True
        agent = db.get(Agent, DEFAULT_AGENT_ID)
        if agent is None:
            db.add(Agent(
                id=DEFAULT_AGENT_ID,
                name=DEFAULT_AGENT_NAME,
                description=DEFAULT_AGENT_DESCRIPTION,
                system_prompt=DEFAULT_AGENT_SYSTEM_PROMPT,
                workspace_id=DEFAULT_WORKSPACE_ID,
                mode="auto",
                workflow_profile_id="auto",
                enabled=True,
                is_default=True,
            ))
        else:
            agent.is_default = True
            agent.system_prompt = DEFAULT_AGENT_SYSTEM_PROMPT
        # Always repair the system-owned coordinator, including legacy rows
        # whose fields were changed before the fixed-master policy existed.
        # SessionLocal deliberately has autoflush disabled, so flush a newly
        # added coordinator before retrieving it for the common repair path.
        db.flush()
        agent = db.get(Agent, DEFAULT_AGENT_ID)
        assert agent is not None
        agent.name = DEFAULT_AGENT_NAME
        agent.description = DEFAULT_AGENT_DESCRIPTION
        agent.system_prompt = DEFAULT_AGENT_SYSTEM_PROMPT
        agent.workspace_id = DEFAULT_WORKSPACE_ID
        agent.model_connection_id = None
        agent.model_id = None
        agent.thinking_level = "auto"
        agent.mode = "auto"
        agent.workflow_profile_id = "auto"
        agent.enabled = True
        agent.is_default = True

        if db.get(MemorySettings, "global") is None:
            db.add(MemorySettings(id="global", enabled=True))

        # The system-owned coordinator always advertises the complete built-in
        # catalog.  User-created Agents keep their own persisted selections.
        db.execute(delete(AgentTool).where(AgentTool.agent_id == DEFAULT_AGENT_ID))
        db.add_all(AgentTool(agent_id=DEFAULT_AGENT_ID, tool_id=tool_id) for tool_id in BUILTIN_TOOL_IDS)

        # Older databases may contain rows marked as default by a previous
        # implementation. Those are user-created child profiles, so make them
        # editable again without deleting their configuration.
        db.execute(
            update(Agent)
            .where(Agent.id != DEFAULT_AGENT_ID, Agent.is_default.is_(True))
            .values(is_default=False)
        )

        # Keep all historic sessions/messages, but make the fixed coordinator
        # their primary Agent from now on. Runs and messages are untouched.
        db.execute(
            update(Session)
            .where(or_(Session.agent_id.is_(None), Session.agent_id != DEFAULT_AGENT_ID))
            .values(agent_id=DEFAULT_AGENT_ID)
        )
        db.commit()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _drop_retired_team_collaboration_tables()
    _migrate_retired_context_storage()
    _migrate_sqlite_columns()
    _drop_retired_session_context_columns()
    _migrate_sqlite_indexes()
    _seed_defaults()


def get_db() -> Generator[OrmSession, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
