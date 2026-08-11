"""PGAgent relational persistence.

The application database deliberately contains only references to provider
secrets.  Actual API keys are handled by :mod:`app.secrets`.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    event,
    inspect,
    or_,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session as OrmSession, mapped_column, relationship, sessionmaker

from app.capabilities import BUILTIN_TOOL_IDS


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "pgagent.db"
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"
DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_AGENT_ID = "00000000-0000-0000-0000-000000000002"
DEFAULT_WORKSPACE_NAME = "一次性任务"
DEFAULT_WORKSPACE_DESCRIPTION = "未选择项目目录的单次任务会话归属。"
DEFAULT_AGENT_NAME = "PGAgent 主控"
DEFAULT_AGENT_DESCRIPTION = "固定主控：理解意图、分析编排、汇总结果并决定继续执行或输出。"
DEFAULT_AGENT_SYSTEM_PROMPT = """你是 PGAgent 主控，负责整个会话的协调与交付。

你的职责是：理解用户意图与约束，判断任务难度，必要时形成清晰计划，汇总已经获得的证据与结果，并决定应继续推进还是直接给出结果。简单、明确且可安全完成的任务可以由你直接完成。

用户创建的 Agent 是可供委派的子 Agent 配置；当系统在 Agent 配置中提供“可委派的子 Agent”列表时，你可以且只能用 task 工具把明确子任务交给其中的精确 agent_id。不要声称已经调用了不存在、未启用或未完成的子 Agent、工具或 Skill；应先确认工具返回的结构化结果，再汇总给用户。遇到没有合适子 Agent 的复杂或专业任务时，先完成你能够可靠完成的分析、规划或结果整理。

始终以用户目标为中心；在不确定、可能破坏数据或需要额外授权时先说明原因。输出应区分已验证事实、推断和下一步建议。"""


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Workspace(TimestampMixin, Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ModelConnection(TimestampMixin, Base):
    __tablename__ = "model_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), default="openai_compatible", nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    discovered_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    manual_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    default_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thinking_level: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    custom_headers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="unchecked", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Skill(TimestampMixin, Base):
    """A locally managed, inert Skill package.

    Installation only copies declared files into the application data folder.
    The runtime does not execute those files merely because this record exists.
    """

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="local", nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    model_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("model_connections.id", ondelete="SET NULL"), nullable=True
    )
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thinking_level: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tool_bindings: Mapped[list["AgentTool"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", lazy="selectin"
    )
    skill_bindings: Mapped[list["AgentSkill"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def tool_ids(self) -> list[str]:
        return sorted(binding.tool_id for binding in self.tool_bindings)

    @property
    def tools(self) -> list["AgentTool"]:
        """Compatibility alias for consumers that expect an Agent.tools relation."""

        return self.tool_bindings

    @property
    def skill_ids(self) -> list[str]:
        return sorted(binding.skill_id for binding in self.skill_bindings)


class AgentTool(Base):
    """Persisted catalog selections for a user-configured Agent."""

    __tablename__ = "agent_tools"

    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    tool_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    agent: Mapped[Agent] = relationship(back_populates="tool_bindings")


class AgentSkill(Base):
    __tablename__ = "agent_skills"

    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skills.id", ondelete="RESTRICT"), primary_key=True
    )
    agent: Mapped[Agent] = relationship(back_populates="skill_bindings")


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), default="New session", nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    model_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("model_connections.id", ondelete="SET NULL"), nullable=True
    )
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thinking_level: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    permission_mode: Mapped[str] = mapped_column(String(16), default="smart", nullable=False)
    context_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    context_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_compacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    skill_bindings: Mapped[list["SessionSkill"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def skill_ids(self) -> list[str]:
        return sorted(binding.skill_id for binding in self.skill_bindings)


class SessionSkill(Base):
    __tablename__ = "session_skills"

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skills.id", ondelete="RESTRICT"), primary_key=True
    )
    session: Mapped[Session] = relationship(back_populates="skill_bindings")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="received", index=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    no_progress_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DraftLaunch(TimestampMixin, Base):
    """Durable idempotency record for materialising an unsaved chat draft.

    A draft is deliberately client-only until its first send.  This row binds
    that first-send request to the workspace/session/run created for it, so a
    browser retry can safely return the original resources instead of creating
    another conversation or scheduling another coordinator run.
    """

    __tablename__ = "draft_launches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Memory(TimestampMixin, Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="Memory", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)


class TeamTask(TimestampMixin, Base):
    __tablename__ = "team_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    team_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    parent_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("team_tasks.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="todo", index=True, nullable=False)
    assignee_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    team_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("team_tasks.id", ondelete="CASCADE"), index=True, nullable=True
    )
    sender_agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recipient_agent_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    message_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hop_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UsageRecord(TimestampMixin, Base):
    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), index=True, nullable=True
    )
    model_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("model_connections.id", ondelete="SET NULL"), index=True, nullable=True
    )
    model_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


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
    "agents": {
        "is_default": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "sessions": {
        "model_connection_id": "VARCHAR(36)",
        "model_id": "VARCHAR(255)",
        "thinking_level": "VARCHAR(16) NOT NULL DEFAULT 'auto'",
        "permission_mode": "VARCHAR(16) NOT NULL DEFAULT 'smart'",
        "context_tokens": "INTEGER NOT NULL DEFAULT 0",
        "last_compacted_at": "DATETIME",
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
        agent.enabled = True
        agent.is_default = True

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
    _migrate_sqlite_columns()
    _seed_defaults()


def get_db() -> Generator[OrmSession, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
