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
    Index,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    func,
    inspect,
    or_,
    select,
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
    context_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    skill_bindings: Mapped[list["SessionSkill"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )
    conversation_compactions: Mapped[list["ConversationCompaction"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin",
        order_by="ConversationCompaction.source_sequence",
    )
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin",
        order_by="Artifact.created_at",
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


class ConversationCompaction(TimestampMixin, Base):
    """One immutable replacement for an old provider-visible transcript prefix.

    ``ChatMessage`` remains the append-only source of truth.  The newest row
    supplies exactly one compacted continuation message plus a verbatim atomic
    tail; messages after ``source_sequence`` are appended during reconstruction.
    """

    __tablename__ = "conversation_compactions"
    __table_args__ = (
        UniqueConstraint("session_id", "source_sequence", name="uq_compaction_session_sequence"),
        Index("ix_compaction_session_sequence", "session_id", "source_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    active_request: Mapped[str] = mapped_column(Text, default="", nullable=False)
    todo_state: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    continuation_messages: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    transcript_artifact: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    artifact_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    reason: Mapped[str] = mapped_column(String(48), default="threshold", nullable=False)
    before_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    after_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    removed_message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_model: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    session: Mapped[Session] = relationship(back_populates="conversation_compactions")


class Artifact(TimestampMixin, Base):
    """Externalized large content referenced from an active context.

    Tool output, files, images, and child-agent transcripts can be stored as
    artifacts.  The prompt keeps only ``id``/``preview``/metadata, so context
    compaction never needs to inline the entire payload again.
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_session_kind", "session_id", "kind"),
        Index("ix_artifacts_session_sha256", "session_id", "sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(40), default="tool_output", index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="artifact", nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    preview: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="available", index=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[Session | None] = relationship(back_populates="artifacts")


class ConversationTurn(Base):
    """Durable delivery receipt for one accepted user message.

    Runtime goals and delegated tasks may fan out below this row, but the
    user-facing contract stays one turn -> one terminal assistant reply.
    Message/run foreign keys point back to this row. The message identifiers
    here are denormalized lookup pointers so incremental SQLite upgrades do
    not require circular foreign keys.
    """

    __tablename__ = "conversation_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "client_message_id", name="uq_conversation_turn_client_message"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    client_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    user_message_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    terminal_message_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(36), default=new_id, unique=True, index=True, nullable=False)
    execution_status: Mapped[str] = mapped_column(String(32), default="received", index=True, nullable=False)
    reply_status: Mapped[str] = mapped_column(String(24), default="pending", index=True, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    message_kind: Mapped[str] = mapped_column(String(24), default="transcript", nullable=False)
    # Only terminal user-facing replies populate this key. Its uniqueness is
    # the database-level exactly-once guard for racing terminal paths.
    terminal_for_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    # Monotonic per-session transcript cursor.  It is the compaction boundary;
    # timestamps remain display metadata only.
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    # Provider-only replay fields (for example DeepSeek reasoning_content).
    # They are deliberately separate from public message metadata so the UI
    # never exposes a raw chain of thought while follow-up requests can replay
    # the provider's exact assistant message.
    provider_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


def next_chat_message_sequence(db: OrmSession, session_id: str) -> int:
    """Return the next per-session transcript sequence for a pending insert."""

    current = db.scalar(
        select(func.max(ChatMessage.sequence)).where(ChatMessage.session_id == session_id)
    )
    return int(current or 0) + 1


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
    # Root runs created from a user message own a turn. Delegated child runs
    # deliberately leave this NULL and report into their parent instead.
    turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), unique=True, index=True, nullable=True
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


class DelegatedTask(TimestampMixin, Base):
    """A child-Agent execution owned by one parent conversation.

    It deliberately has no team, lease, claim, or inter-agent-message fields:
    child work belongs only to its parent run and conversation.
    """

    __tablename__ = "delegated_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parent_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    parent_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    child_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    child_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="in_progress", index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


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
            if has_unique_constraint("runs", "turn_id"):
                connection.exec_driver_sql('DROP INDEX IF EXISTS "uq_runs_turn_id"')
            elif not has_unique_index("runs", "turn_id"):
                connection.exec_driver_sql(
                    'CREATE UNIQUE INDEX "uq_runs_turn_id" '
                    'ON "runs" ("turn_id") WHERE "turn_id" IS NOT NULL'
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
