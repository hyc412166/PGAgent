"""SQLAlchemy ORM entities for PGAgent's relational state."""

from __future__ import annotations

from datetime import datetime

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
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session as OrmSession, mapped_column, relationship

from src.persistence.base import Base, TimestampMixin, new_id, utcnow

class Workspace(TimestampMixin, Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    validation_runtime: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class ModelConnection(TimestampMixin, Base):
    __tablename__ = "model_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), default="openai_compatible", nullable=False)
    # Existing rows are migrated to Chat Completions; newly created rows set
    # their explicit protocol from the API request (Responses by default).
    api_protocol: Mapped[str] = mapped_column(String(32), default="chat_completions", nullable=False)
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
    workflow_profile_id: Mapped[str] = mapped_column(
        String(16), default="auto", nullable=False
    )
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
    use_memories: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mcp_server_names: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
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
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("durable_tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    plan_step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="SET NULL"), index=True, nullable=True
    )
    run_kind: Mapped[str] = mapped_column(String(24), default="initial", nullable=False)
    resumed_from_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
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


class DurableTask(TimestampMixin, Base):
    """A durable user goal that may span multiple model runs."""

    __tablename__ = "durable_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    origin_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="planning", index=True, nullable=False)
    active_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resume_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlanStep(TimestampMixin, Base):
    """One semantic plan step whose progress survives process loss."""

    __tablename__ = "plan_steps"
    __table_args__ = (Index("ix_plan_steps_task_position", "task_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("durable_tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True, nullable=False)
    executor_kind: Mapped[str] = mapped_column(String(24), default="main", index=True, nullable=False)
    assigned_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # Informational backlink only, matching ``last_run_id`` below. Run already
    # points at PlanStep; a reverse SQLite FK would create a DDL cycle.
    assigned_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    claim_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    workspace_mode: Mapped[str] = mapped_column(String(24), default="shared", nullable=False)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_work: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    remaining_work: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    next_action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    result: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Informational backlink only. Keeping this as a plain indexed identifier
    # avoids a runs <-> plan_steps DDL cycle while Run owns the live FK.
    last_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlanStepDependency(Base):
    """A directed edge from one plan step to a prerequisite step."""

    __tablename__ = "plan_step_dependencies"
    __table_args__ = (
        UniqueConstraint("step_id", "depends_on_step_id", name="uq_plan_step_dependency_edge"),
        Index("ix_plan_step_dependencies_prerequisite", "depends_on_step_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    step_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="CASCADE"), index=True, nullable=False
    )
    depends_on_step_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="CASCADE"), nullable=False
    )


class CollaborationEvent(Base):
    """Durable completion/message event consumed by collaboration runtimes."""

    __tablename__ = "collaboration_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("durable_tasks.id", ondelete="CASCADE"), index=True, nullable=True
    )
    plan_step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="CASCADE"), index=True, nullable=True
    )
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    source_kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    source_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumer_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CollaborationTeam(TimestampMixin, Base):
    __tablename__ = "collaboration_teams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parent_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("durable_tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(160), default="Agent team", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True, nullable=False)


class TeammateWorker(TimestampMixin, Base):
    __tablename__ = "teammate_workers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    team_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collaboration_teams.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(160), default="teammate", nullable=False)
    prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="idle", index=True, nullable=False)
    current_plan_step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="SET NULL"), index=True, nullable=True
    )
    last_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    workspace_mode: Mapped[str] = mapped_column(String(24), default="shared", nullable=False)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class CollaborationMessage(Base):
    __tablename__ = "collaboration_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    team_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collaboration_teams.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sender_worker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("teammate_workers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    recipient_worker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("teammate_workers.id", ondelete="CASCADE"), index=True, nullable=True
    )
    message_type: Mapped[str] = mapped_column(String(32), default="message", index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


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


class MemorySettings(TimestampMixin, Base):
    """The process-wide persistent-memory preference."""

    __tablename__ = "memory_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Memory(TimestampMixin, Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="Memory", index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="Memory", nullable=False)
    memory_type: Mapped[str] = mapped_column(String(24), default="project", index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True, nullable=False)
    source_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    superseded_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="SET NULL"), index=True, nullable=True
    )
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_usage_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    consolidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)


class MemoryRollout(TimestampMixin, Base):
    """One Phase-1, retrieval-oriented extraction from an accepted root turn."""

    __tablename__ = "memory_rollouts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), index=True, nullable=True
    )
    extraction_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("memory_jobs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    source_end_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    cwd: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rollout_slug: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    rollout_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    raw_memories: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    task_groups: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), default="uncertain", index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True, nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_usage_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    selected_for_phase2_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consolidation_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("memory_jobs.id", ondelete="SET NULL"), index=True, nullable=True
    )


class MemoryCitation(Base):
    """An accepted run's durable acknowledgement that it used one memory source."""

    __tablename__ = "memory_citations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    target_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MemorySkill(TimestampMixin, Base):
    """A repeated, evidence-backed procedure promoted by Phase 2."""

    __tablename__ = "memory_skills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_rollout_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True, nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_usage_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)


class MemoryJob(TimestampMixin, Base):
    """Durable auxiliary model work used to extract persistent memories."""

    __tablename__ = "memory_jobs"
    __table_args__ = (UniqueConstraint("run_id", "kind", name="uq_memory_jobs_run_kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(24), default="extract", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class BackgroundJob(TimestampMixin, Base):
    """A durable command launched by an Agent run and observed before reply."""

    __tablename__ = "background_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), index=True, nullable=True
    )
    plan_step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="SET NULL"), index=True, nullable=True
    )
    workspace_root: Mapped[str] = mapped_column(Text, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    shell: Mapped[str] = mapped_column(String(24), default="command", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    output_preview: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_by_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    waiting_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)


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
    plan_step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="SET NULL"), index=True, nullable=True
    )
    teammate_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("teammate_workers.id", ondelete="SET NULL"), index=True, nullable=True
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
