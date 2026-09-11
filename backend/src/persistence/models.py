"""SQLAlchemy ORM entities for PGAgent's relational state."""
# 文件职责：负责数据库模型、默认数据与事务访问中的 models 子模块。
# 逻辑关系：上层通过 persistence/models.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

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

# 类职责：定义 Workspace 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class Workspace(TimestampMixin, Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "workspaces"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：name 表示当前对象名称。
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：root_path 表示root_path 对应的文件系统位置。
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 变量说明：validation_runtime 表示当前步骤使用的 validation_runtime 值。
    validation_runtime: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


# 类职责：定义 ModelConnection 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ModelConnection(TimestampMixin, Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "model_connections"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：name 表示当前对象名称。
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    # 变量说明：provider 表示模型供应商。
    provider: Mapped[str] = mapped_column(String(50), default="openai_compatible", nullable=False)
    # Existing rows are migrated to Chat Completions; newly created rows set
    # their explicit protocol from the API request (Responses by default).
    # 变量说明：api_protocol 表示当前步骤使用的 api_protocol 值。
    api_protocol: Mapped[str] = mapped_column(String(32), default="chat_completions", nullable=False)
    # 变量说明：base_url 表示base 的访问地址。
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：secret_ref 表示当前步骤使用的 secret_ref 值。
    secret_ref: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # 变量说明：discovered_models 表示当前流程使用的 discovered_models 集合。
    discovered_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：manual_models 表示当前流程使用的 manual_models 集合。
    manual_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # disabled_models 只保存用户主动关闭的模型；空列表让现有连接升级后继续保持全部可用。
    disabled_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：default_model 表示当前步骤使用的 default_model 值。
    default_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    # 变量说明：custom_headers 表示当前流程使用的 custom_headers 集合。
    custom_headers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    # 变量说明：capabilities 表示当前流程使用的 capabilities 集合。
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(32), default="unchecked", nullable=False)
    # 变量说明：last_error 表示当前步骤使用的 last_error 值。
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变量说明：last_checked_at 表示last_checked_at 对应的时间信息。
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# 类职责：定义 Skill 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class Skill(TimestampMixin, Base):
    """A locally managed, inert Skill package.

    Installation only copies declared files into the application data folder.
    The runtime does not execute those files merely because this record exists.
    """

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "skills"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：slug 表示当前步骤使用的 slug 值。
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    # 变量说明：name 表示当前对象名称。
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：source 表示当前步骤使用的 source 值。
    source: Mapped[str] = mapped_column(String(32), default="local", nullable=False)
    # 变量说明：source_url 表示source 的访问地址。
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变量说明：root_path 表示root_path 对应的文件系统位置。
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：version 表示当前步骤使用的 version 值。
    version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 变量说明：installed_at 表示installed_at 对应的时间信息。
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# 类职责：定义 Agent 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class Agent(TimestampMixin, Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "agents"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：name 表示当前对象名称。
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：system_prompt 表示当前步骤使用的 system_prompt 值。
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("model_connections.id", ondelete="SET NULL"), nullable=True
    )
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    # 变量说明：mode 表示当前步骤使用的 mode 值。
    mode: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    # 变量说明：workflow_profile_id 表示workflow_profile 对象的唯一标识。
    workflow_profile_id: Mapped[str] = mapped_column(
        String(16), default="auto", nullable=False
    )
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 变量说明：is_default 表示表示是否满足 default 条件的布尔标记。
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 变量说明：tool_bindings 表示当前流程使用的 tool_bindings 集合。
    tool_bindings: Mapped[list["AgentTool"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", lazy="selectin"
    )
    # 变量说明：skill_bindings 表示当前流程使用的 skill_bindings 集合。
    skill_bindings: Mapped[list["AgentSkill"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", lazy="selectin"
    )

    # 函数职责：完成 tool_ids 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def tool_ids(self) -> list[str]:
        return sorted(binding.tool_id for binding in self.tool_bindings)

    # 函数职责：完成 tools 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def tools(self) -> list["AgentTool"]:
        """Compatibility alias for consumers that expect an Agent.tools relation."""

        return self.tool_bindings

    # 函数职责：完成 skill_ids 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def skill_ids(self) -> list[str]:
        return sorted(binding.skill_id for binding in self.skill_bindings)


# 类职责：定义 AgentTool 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class AgentTool(Base):
    """Persisted catalog selections for a user-configured Agent."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "agent_tools"

    # 变量说明：agent_id 表示智能体标识。
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    # 变量说明：tool_id 表示tool 对象的唯一标识。
    tool_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    # 变量说明：agent 表示当前步骤使用的 agent 值。
    agent: Mapped[Agent] = relationship(back_populates="tool_bindings")


# 类职责：定义 AgentSkill 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class AgentSkill(Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "agent_skills"

    # 变量说明：agent_id 表示智能体标识。
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    # 变量说明：skill_id 表示skill 对象的唯一标识。
    skill_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skills.id", ondelete="RESTRICT"), primary_key=True
    )
    # 变量说明：agent 表示当前步骤使用的 agent 值。
    agent: Mapped[Agent] = relationship(back_populates="skill_bindings")


# 类职责：定义 Session 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class Session(TimestampMixin, Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "sessions"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: Mapped[str] = mapped_column(String(200), default="New session", nullable=False)
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    # 变量说明：agent_id 表示智能体标识。
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("model_connections.id", ondelete="SET NULL"), nullable=True
    )
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    # 变量说明：permission_mode 表示当前步骤使用的 permission_mode 值。
    permission_mode: Mapped[str] = mapped_column(String(16), default="smart", nullable=False)
    # 变量说明：use_memories 表示当前流程使用的 use_memories 集合。
    use_memories: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 变量说明：mcp_server_names 表示当前流程使用的 mcp_server_names 集合。
    mcp_server_names: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：context_tokens 表示当前流程使用的 context_tokens 集合。
    context_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    # 变量说明：skill_bindings 表示当前流程使用的 skill_bindings 集合。
    skill_bindings: Mapped[list["SessionSkill"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )
    # 变量说明：conversation_compactions 表示当前流程使用的 conversation_compactions 集合。
    conversation_compactions: Mapped[list["ConversationCompaction"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin",
        order_by="ConversationCompaction.source_sequence",
    )
    # 变量说明：artifacts 表示当前流程使用的 artifacts 集合。
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin",
        order_by="Artifact.created_at",
    )

    # 函数职责：完成 skill_ids 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def skill_ids(self) -> list[str]:
        return sorted(binding.skill_id for binding in self.skill_bindings)


# 类职责：定义 SessionSkill 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SessionSkill(Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "session_skills"

    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    # 变量说明：skill_id 表示skill 对象的唯一标识。
    skill_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skills.id", ondelete="RESTRICT"), primary_key=True
    )
    # 变量说明：session 表示当前步骤使用的 session 值。
    session: Mapped[Session] = relationship(back_populates="skill_bindings")


# 类职责：定义 ConversationCompaction 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ConversationCompaction(TimestampMixin, Base):
    """One immutable replacement for an old provider-visible transcript prefix.

    ``ChatMessage`` remains the append-only source of truth.  The newest row
    supplies exactly one compacted continuation message plus a verbatim atomic
    tail; messages after ``source_sequence`` are appended during reconstruction.
    """

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "conversation_compactions"
    # 变量说明：__table_args__ 表示当前步骤使用的 __table_args__ 值。
    __table_args__ = (
        UniqueConstraint("session_id", "source_sequence", name="uq_compaction_session_sequence"),
        Index("ix_compaction_session_sequence", "session_id", "source_sequence"),
    )

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：source_run_id 表示source_run 对象的唯一标识。
    source_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：source_sequence 表示当前步骤使用的 source_sequence 值。
    source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # 变量说明：active_request 表示当前步骤使用的 active_request 值。
    active_request: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：todo_state 表示当前步骤使用的 todo_state 值。
    todo_state: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：summary 表示当前步骤使用的 summary 值。
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：continuation_messages 表示当前流程使用的 continuation_messages 集合。
    continuation_messages: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：transcript_artifact 表示当前步骤使用的 transcript_artifact 值。
    transcript_artifact: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 变量说明：artifact_refs 表示当前流程使用的 artifact_refs 集合。
    artifact_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: Mapped[str] = mapped_column(String(48), default="threshold", nullable=False)
    # 变量说明：before_tokens 表示当前流程使用的 before_tokens 集合。
    before_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：after_tokens 表示当前流程使用的 after_tokens 集合。
    after_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：removed_message_count 表示removed_message 的数量。
    removed_message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：used_model 表示当前步骤使用的 used_model 值。
    used_model: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 变量说明：fallback 表示当前步骤使用的 fallback 值。
    fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # 变量说明：session 表示当前步骤使用的 session 值。
    session: Mapped[Session] = relationship(back_populates="conversation_compactions")


# 类职责：定义 Artifact 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class Artifact(TimestampMixin, Base):
    """Externalized large content referenced from an active context.

    Tool output, files, images, and child-agent transcripts can be stored as
    artifacts.  The prompt keeps only ``id``/``preview``/metadata, so context
    compaction never needs to inline the entire payload again.
    """

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "artifacts"
    # 变量说明：__table_args__ 表示当前步骤使用的 __table_args__ 值。
    __table_args__ = (
        Index("ix_artifacts_session_kind", "session_id", "kind"),
        Index("ix_artifacts_session_sha256", "session_id", "sha256"),
    )

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # 变量说明：kind 表示当前步骤使用的 kind 值。
    kind: Mapped[str] = mapped_column(String(40), default="tool_output", index=True, nullable=False)
    # 变量说明：name 表示当前对象名称。
    name: Mapped[str] = mapped_column(String(255), default="artifact", nullable=False)
    # 变量说明：storage_path 表示storage_path 对应的文件系统位置。
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：sha256 表示当前步骤使用的 sha256 值。
    sha256: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # 变量说明：mime_type 表示当前步骤使用的 mime_type 值。
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # 变量说明：size_bytes 表示当前流程使用的 size_bytes 集合。
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：preview 表示当前步骤使用的 preview 值。
    preview: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：source_event_id 表示source_event 对象的唯一标识。
    source_event_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # 变量说明：metadata_json 表示metadata 的 JSON 序列化内容。
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(24), default="available", index=True, nullable=False)
    # 变量说明：expires_at 表示expires_at 对应的时间信息。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 变量说明：session 表示当前步骤使用的 session 值。
    session: Mapped[Session | None] = relationship(back_populates="artifacts")


# 类职责：定义 ConversationTurn 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ConversationTurn(Base):
    """Durable delivery receipt for one accepted user message.

    Runtime goals and delegated tasks may fan out below this row, but the
    user-facing contract stays one turn -> one terminal assistant reply.
    Message/run foreign keys point back to this row. The message identifiers
    here are denormalized lookup pointers so incremental SQLite upgrades do
    not require circular foreign keys.
    """

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "conversation_turns"
    # 变量说明：__table_args__ 表示当前步骤使用的 __table_args__ 值。
    __table_args__ = (
        UniqueConstraint("session_id", "client_message_id", name="uq_conversation_turn_client_message"),
    )

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：client_message_id 表示client_message 对象的唯一标识。
    client_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 变量说明：request_fingerprint 表示当前步骤使用的 request_fingerprint 值。
    request_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # 变量说明：user_message_id 表示user_message 对象的唯一标识。
    user_message_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    # 变量说明：terminal_message_id 表示terminal_message 对象的唯一标识。
    terminal_message_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    # 变量说明：trace_id 表示trace 对象的唯一标识。
    trace_id: Mapped[str] = mapped_column(String(36), default=new_id, unique=True, index=True, nullable=False)
    # 变量说明：execution_status 表示当前流程使用的 execution_status 集合。
    execution_status: Mapped[str] = mapped_column(String(32), default="received", index=True, nullable=False)
    # 变量说明：reply_status 表示当前流程使用的 reply_status 集合。
    reply_status: Mapped[str] = mapped_column(String(24), default="pending", index=True, nullable=False)
    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 变量说明：error_message 表示当前步骤使用的 error_message 值。
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变量说明：accepted_at 表示accepted_at 对应的时间信息。
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # 变量说明：heartbeat_at 表示heartbeat_at 对应的时间信息。
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # 变量说明：finished_at 表示finished_at 对应的时间信息。
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 类职责：定义 ChatMessage 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ChatMessage(Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "chat_messages"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：role 表示当前步骤使用的 role 值。
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    # 变量说明：content 表示待处理或返回的正文内容。
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 变量说明：tool_call_id 表示tool_call 对象的唯一标识。
    tool_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 变量说明：turn_id 表示turn 对象的唯一标识。
    turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：message_kind 表示当前步骤使用的 message_kind 值。
    message_kind: Mapped[str] = mapped_column(String(24), default="transcript", nullable=False)
    # Only terminal user-facing replies populate this key. Its uniqueness is
    # the database-level exactly-once guard for racing terminal paths.
    # 变量说明：terminal_for_turn_id 表示terminal_for_turn 对象的唯一标识。
    terminal_for_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    # Monotonic per-session transcript cursor.  It is the compaction boundary;
    # timestamps remain display metadata only.
    # 变量说明：sequence 表示当前步骤使用的 sequence 值。
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    # 变量说明：extra 表示当前步骤使用的 extra 值。
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)
    # Provider-only replay fields (for example DeepSeek reasoning_content).
    # They are deliberately separate from public message metadata so the UI
    # never exposes a raw chain of thought while follow-up requests can replay
    # the provider's exact assistant message.
    # 变量说明：provider_payload 表示当前步骤使用的 provider_payload 值。
    provider_payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 变量说明：created_at 表示创建时间。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    # 函数职责：完成 citations 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def citations(self) -> list[dict[str, Any]]:
        # 变量说明：native 表示当前步骤使用的 native 值。
        native = self.provider_payload.get("native") if isinstance(self.provider_payload, dict) else None
        # 变量说明：items 表示待处理的元素集合。
        items = native.get("items") if isinstance(native, dict) else None
        # 变量说明：citations 表示当前流程使用的 citations 集合。
        citations: list[dict[str, Any]] = []
        # 变量说明：seen_urls 表示当前流程使用的 seen_urls 集合。
        seen_urls: set[str] = set()
        for item in items or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                for annotation in part.get("annotations") or []:
                    if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                        continue
                    # 变量说明：url 表示当前步骤使用的 url 值。
                    url = str(annotation.get("url") or "")[:2_048]
                    try:
                        # 变量说明：parsed 表示当前步骤使用的 parsed 值。
                        parsed = urlsplit(url)
                        # 变量说明：valid 表示当前步骤使用的 valid 值。
                        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                    except ValueError:
                        # 变量说明：valid 表示当前步骤使用的 valid 值。
                        valid = False
                    if not valid or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    citations.append({"url": url, "title": str(annotation.get("title") or url)[:500]})
        return citations


# 函数职责：完成 next_chat_message_sequence 对应的业务处理。
# 参数关系：db 表示当前数据库会话；session_id 表示所属会话标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def next_chat_message_sequence(db: OrmSession, session_id: str) -> int:
    """Return the next per-session transcript sequence for a pending insert."""

    # 变量说明：current 表示当前步骤使用的 current 值。
    current = db.scalar(
        select(func.max(ChatMessage.sequence)).where(ChatMessage.session_id == session_id)
    )
    return int(current or 0) + 1


# 类职责：定义 Run 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class Run(Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "runs"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    # 变量说明：agent_id 表示智能体标识。
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    # Root runs created from a user message own a turn. Delegated child runs
    # deliberately leave this NULL and report into their parent instead.
    # 变量说明：turn_id 表示turn 对象的唯一标识。
    turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), unique=True, index=True, nullable=True
    )
    # 变量说明：task_id 表示任务标识。
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("durable_tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
    plan_step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：run_kind 表示当前步骤使用的 run_kind 值。
    run_kind: Mapped[str] = mapped_column(String(24), default="initial", nullable=False)
    # 变量说明：resumed_from_run_id 表示resumed_from_run 对象的唯一标识。
    resumed_from_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(32), default="received", index=True, nullable=False)
    # 变量说明：mode 表示当前步骤使用的 mode 值。
    mode: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    # 变量说明：current_step 表示当前步骤使用的 current_step 值。
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：tool_calls 表示当前流程使用的 tool_calls 集合。
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：no_progress_steps 表示当前流程使用的 no_progress_steps 集合。
    no_progress_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
    stop_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 变量说明：error_message 表示当前步骤使用的 error_message 值。
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变量说明：started_at 表示started_at 对应的时间信息。
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # 变量说明：finished_at 表示finished_at 对应的时间信息。
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 类职责：定义 DurableTask 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class DurableTask(TimestampMixin, Base):
    """A durable user goal that may span multiple model runs."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "durable_tasks"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：origin_turn_id 表示origin_turn 对象的唯一标识。
    origin_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：goal 表示当前步骤使用的 goal 值。
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：constraints 表示当前流程使用的 constraints 集合。
    constraints: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(32), default="planning", index=True, nullable=False)
    # 变量说明：active_step_id 表示active_step 对象的唯一标识。
    active_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
    resume_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：completed_at 表示completed_at 对应的时间信息。
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 类职责：定义 PlanStep 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class PlanStep(TimestampMixin, Base):
    """One semantic plan step whose progress survives process loss."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "plan_steps"
    # 变量说明：__table_args__ 表示当前步骤使用的 __table_args__ 值。
    __table_args__ = (Index("ix_plan_steps_task_position", "task_id", "position"),)

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：task_id 表示任务标识。
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("durable_tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：external_id 表示external 对象的唯一标识。
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # 变量说明：position 表示当前步骤使用的 position 值。
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True, nullable=False)
    # 变量说明：executor_kind 表示当前步骤使用的 executor_kind 值。
    executor_kind: Mapped[str] = mapped_column(String(24), default="main", index=True, nullable=False)
    # 变量说明：assigned_agent_id 表示assigned_agent 对象的唯一标识。
    assigned_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # Informational backlink only, matching ``last_run_id`` below. Run already
    # points at PlanStep; a reverse SQLite FK would create a DDL cycle.
    # 变量说明：assigned_run_id 表示assigned_run 对象的唯一标识。
    assigned_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # 变量说明：claim_owner 表示当前步骤使用的 claim_owner 值。
    claim_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # 变量说明：workspace_mode 表示当前步骤使用的 workspace_mode 值。
    workspace_mode: Mapped[str] = mapped_column(String(24), default="shared", nullable=False)
    # 变量说明：worktree_path 表示worktree_path 对应的文件系统位置。
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变量说明：attempt 表示当前步骤使用的 attempt 值。
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：error 表示当前捕获或准备上报的错误。
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变量说明：completed_work 表示当前步骤使用的 completed_work 值。
    completed_work: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：remaining_work 表示当前步骤使用的 remaining_work 值。
    remaining_work: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：next_action 表示当前步骤使用的 next_action 值。
    next_action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：result 表示本步骤产生的结果。
    result: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：evidence 表示当前步骤使用的 evidence 值。
    evidence: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Informational backlink only. Keeping this as a plain indexed identifier
    # avoids a runs <-> plan_steps DDL cycle while Run owns the live FK.
    # 变量说明：last_run_id 表示last_run 对象的唯一标识。
    last_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # 变量说明：started_at 表示started_at 对应的时间信息。
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 变量说明：completed_at 表示completed_at 对应的时间信息。
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 类职责：定义 PlanStepDependency 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class PlanStepDependency(Base):
    """A directed edge from one plan step to a prerequisite step."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "plan_step_dependencies"
    # 变量说明：__table_args__ 表示当前步骤使用的 __table_args__ 值。
    __table_args__ = (
        UniqueConstraint("step_id", "depends_on_step_id", name="uq_plan_step_dependency_edge"),
        Index("ix_plan_step_dependencies_prerequisite", "depends_on_step_id"),
    )

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：step_id 表示step 对象的唯一标识。
    step_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：depends_on_step_id 表示depends_on_step 对象的唯一标识。
    depends_on_step_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="CASCADE"), nullable=False
    )


# 类职责：定义 CollaborationEvent 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class CollaborationEvent(Base):
    """Durable completion/message event consumed by collaboration runtimes."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "collaboration_events"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：task_id 表示任务标识。
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("durable_tasks.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
    plan_step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # 变量说明：run_id 表示当前运行标识。
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # 变量说明：source_kind 表示当前步骤使用的 source_kind 值。
    source_kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # 变量说明：source_id 表示source 对象的唯一标识。
    source_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    # 变量说明：event_type 表示当前步骤使用的 event_type 值。
    event_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 变量说明：consumed_at 表示consumed_at 对应的时间信息。
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 变量说明：consumer_run_id 表示consumer_run 对象的唯一标识。
    consumer_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # 变量说明：created_at 表示创建时间。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# 类职责：定义 CollaborationTeam 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class CollaborationTeam(TimestampMixin, Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "collaboration_teams"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
    parent_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：task_id 表示任务标识。
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("durable_tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：name 表示当前对象名称。
    name: Mapped[str] = mapped_column(String(160), default="Agent team", nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(24), default="active", index=True, nullable=False)


# 类职责：定义 TeammateWorker 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class TeammateWorker(TimestampMixin, Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "teammate_workers"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：team_id 表示team 对象的唯一标识。
    team_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collaboration_teams.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：agent_id 表示智能体标识。
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    # 变量说明：name 表示当前对象名称。
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # 变量说明：role 表示当前步骤使用的 role 值。
    role: Mapped[str] = mapped_column(String(160), default="teammate", nullable=False)
    # 变量说明：prompt 表示当前步骤使用的 prompt 值。
    prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(24), default="idle", index=True, nullable=False)
    # 变量说明：current_plan_step_id 表示current_plan_step 对象的唯一标识。
    current_plan_step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：last_run_id 表示last_run 对象的唯一标识。
    last_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # 变量说明：workspace_mode 表示当前步骤使用的 workspace_mode 值。
    workspace_mode: Mapped[str] = mapped_column(String(24), default="shared", nullable=False)
    # 变量说明：worktree_path 表示worktree_path 对应的文件系统位置。
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变量说明：branch_name 表示当前步骤使用的 branch_name 值。
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


# 类职责：定义 CollaborationMessage 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class CollaborationMessage(Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "collaboration_messages"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：team_id 表示team 对象的唯一标识。
    team_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collaboration_teams.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：sender_worker_id 表示sender_worker 对象的唯一标识。
    sender_worker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("teammate_workers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：recipient_worker_id 表示recipient_worker 对象的唯一标识。
    recipient_worker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("teammate_workers.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # 变量说明：message_type 表示当前步骤使用的 message_type 值。
    message_type: Mapped[str] = mapped_column(String(32), default="message", index=True, nullable=False)
    # 变量说明：content 表示待处理或返回的正文内容。
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 变量说明：read_at 表示read_at 对应的时间信息。
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    # 变量说明：created_at 表示创建时间。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# 类职责：定义 DraftLaunch 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class DraftLaunch(TimestampMixin, Base):
    """Durable idempotency record for materialising an unsaved chat draft.

    A draft is deliberately client-only until its first send.  This row binds
    that first-send request to the workspace/session/run created for it, so a
    browser retry can safely return the original resources instead of creating
    another conversation or scheduling another coordinator run.
    """

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "draft_launches"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：idempotency_key 表示当前步骤使用的 idempotency_key 值。
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # 变量说明：request_fingerprint 表示当前步骤使用的 request_fingerprint 值。
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    # 变量说明：run_id 表示当前运行标识。
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )


# 类职责：定义 RunEvent 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class RunEvent(Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "run_events"
    # 按 Run 的复合索引支持持久事件的顺序读取，不把并发分配责任伪装成数据库门禁。
    __table_args__ = (Index("ix_run_events_run_sequence", "run_id", "sequence"),)

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：run_id 表示当前运行标识。
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：event_type 表示当前步骤使用的 event_type 值。
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    # trace_id 与当前运行的可观测上下文关联；历史事件没有该值，因此允许为空。
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # sequence 是每个 Run 内的持久事件顺序；0 仅兼容过渡期未改造的直接 ORM 写入。
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：step 表示当前步骤使用的 step 值。
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 变量说明：created_at 表示创建时间。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# 类职责：定义 Approval 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class Approval(Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "approvals"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：run_id 表示当前运行标识。
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True, nullable=False)
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变量说明：created_at 表示创建时间。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # 变量说明：decided_at 表示decided_at 对应的时间信息。
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 类职责：定义 MemorySettings 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MemorySettings(TimestampMixin, Base):
    """The process-wide persistent-memory preference."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "memory_settings"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# 类职责：定义 Memory 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class Memory(TimestampMixin, Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "memories"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：scope 表示当前步骤使用的 scope 值。
    scope: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    # 变量说明：scope_id 表示scope 对象的唯一标识。
    scope_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    # 变量说明：name 表示当前对象名称。
    name: Mapped[str] = mapped_column(String(200), default="Memory", index=True, nullable=False)
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: Mapped[str] = mapped_column(String(200), default="Memory", nullable=False)
    # 变量说明：memory_type 表示当前步骤使用的 memory_type 值。
    memory_type: Mapped[str] = mapped_column(String(24), default="project", index=True, nullable=False)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：content 表示待处理或返回的正文内容。
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：tags 表示当前流程使用的 tags 集合。
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：pinned 表示当前步骤使用的 pinned 值。
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(24), default="active", index=True, nullable=False)
    # 变量说明：source_session_id 表示source_session 对象的唯一标识。
    source_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：source_turn_id 表示source_turn 对象的唯一标识。
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：superseded_by 表示当前步骤使用的 superseded_by 值。
    superseded_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：usage_count 表示usage 的数量。
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：last_usage_at 表示last_usage_at 对应的时间信息。
    last_usage_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    # 变量说明：consolidated_at 表示consolidated_at 对应的时间信息。
    consolidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 变量说明：extra 表示当前步骤使用的 extra 值。
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict, nullable=False)


# 类职责：定义 MemoryRollout 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MemoryRollout(TimestampMixin, Base):
    """One Phase-1, retrieval-oriented extraction from an accepted root turn."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "memory_rollouts"
    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：source_session_id 表示source_session 对象的唯一标识。
    source_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：source_turn_id 表示source_turn 对象的唯一标识。
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：extraction_job_id 表示extraction_job 对象的唯一标识。
    extraction_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("memory_jobs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：source_end_sequence 表示当前步骤使用的 source_end_sequence 值。
    source_end_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # 变量说明：cwd 表示当前步骤使用的 cwd 值。
    cwd: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：rollout_slug 表示当前步骤使用的 rollout_slug 值。
    rollout_slug: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    # 变量说明：rollout_summary 表示当前步骤使用的 rollout_summary 值。
    rollout_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：raw_memories 表示当前流程使用的 raw_memories 集合。
    raw_memories: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：keywords 表示当前流程使用的 keywords 集合。
    keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：task_groups 表示当前流程使用的 task_groups 集合。
    task_groups: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：outcome 表示当前步骤使用的 outcome 值。
    outcome: Mapped[str] = mapped_column(String(24), default="uncertain", index=True, nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(24), default="active", index=True, nullable=False)
    # 变量说明：usage_count 表示usage 的数量。
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：last_usage_at 表示last_usage_at 对应的时间信息。
    last_usage_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    # 变量说明：selected_for_phase2_at 表示selected_for_phase2_at 对应的时间信息。
    selected_for_phase2_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 变量说明：consolidation_job_id 表示consolidation_job 对象的唯一标识。
    consolidation_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("memory_jobs.id", ondelete="SET NULL"), index=True, nullable=True
    )


# 类职责：定义 MemoryCitation 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MemoryCitation(Base):
    """An accepted run's durable acknowledgement that it used one memory source."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "memory_citations"
    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：run_id 表示当前运行标识。
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：turn_id 表示turn 对象的唯一标识。
    turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversation_turns.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：target_type 表示当前步骤使用的 target_type 值。
    target_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # 变量说明：target_id 表示target 对象的唯一标识。
    target_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # 变量说明：note 表示当前步骤使用的 note 值。
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：created_at 表示创建时间。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# 类职责：定义 MemorySkill 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MemorySkill(TimestampMixin, Base):
    """A repeated, evidence-backed procedure promoted by Phase 2."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "memory_skills"
    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # 变量说明：name 表示当前对象名称。
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：content 表示待处理或返回的正文内容。
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：keywords 表示当前流程使用的 keywords 集合。
    keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：source_rollout_ids 表示source_rollout 对象标识集合。
    source_rollout_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(24), default="active", index=True, nullable=False)
    # 变量说明：usage_count 表示usage 的数量。
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：last_usage_at 表示last_usage_at 对应的时间信息。
    last_usage_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)


# 类职责：定义 MemoryJob 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MemoryJob(TimestampMixin, Base):
    """Durable auxiliary model work used to extract persistent memories."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "memory_jobs"
    # 变量说明：__table_args__ 表示当前步骤使用的 __table_args__ 值。
    __table_args__ = (UniqueConstraint("run_id", "kind", name="uq_memory_jobs_run_kind"),)

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：run_id 表示当前运行标识。
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：kind 表示当前步骤使用的 kind 值。
    kind: Mapped[str] = mapped_column(String(24), default="extract", nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True, nullable=False)
    # 变量说明：attempts 表示当前流程使用的 attempts 集合。
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 变量说明：result 表示本步骤产生的结果。
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 变量说明：error 表示当前捕获或准备上报的错误。
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变量说明：lease_expires_at 表示lease_expires_at 对应的时间信息。
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    # 变量说明：request_count 表示request 的数量。
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：input_tokens 表示当前流程使用的 input_tokens 集合。
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：output_tokens 表示当前流程使用的 output_tokens 集合。
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：cache_creation_tokens 表示当前流程使用的 cache_creation_tokens 集合。
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：cache_read_tokens 表示当前流程使用的 cache_read_tokens 集合。
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：total_tokens 表示当前流程使用的 total_tokens 集合。
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：cost_usd 表示当前步骤使用的 cost_usd 值。
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


# 类职责：定义 BackgroundJob 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class BackgroundJob(TimestampMixin, Base):
    """A durable command launched by an Agent run and observed before reply."""

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "background_jobs"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：run_id 表示当前运行标识。
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
    plan_step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
    workspace_root: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：command 表示当前步骤使用的 command 值。
    command: Mapped[str] = mapped_column(Text, nullable=False)
    # 变量说明：shell 表示当前步骤使用的 shell 值。
    shell: Mapped[str] = mapped_column(String(24), default="command", nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True, nullable=False)
    # 变量说明：timeout_seconds 表示当前流程使用的 timeout_seconds 集合。
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    # 变量说明：pid 表示当前步骤使用的 pid 值。
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 变量说明：exit_code 表示当前步骤使用的 exit_code 值。
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 变量说明：log_path 表示log_path 对应的文件系统位置。
    log_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：output_preview 表示当前步骤使用的 output_preview 值。
    output_preview: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：error 表示当前捕获或准备上报的错误。
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 变量说明：started_at 表示started_at 对应的时间信息。
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 变量说明：finished_at 表示finished_at 对应的时间信息。
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 变量说明：observed_at 表示observed_at 对应的时间信息。
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 变量说明：observed_by_run_id 表示observed_by_run 对象的唯一标识。
    observed_by_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：waiting_run_id 表示waiting_run 对象的唯一标识。
    waiting_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)


# 类职责：定义 DelegatedTask 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class DelegatedTask(TimestampMixin, Base):
    """A child-Agent execution owned by one parent conversation.

    It deliberately has no team, lease, claim, or inter-agent-message fields:
    child work belongs only to its parent run and conversation.
    """

    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "delegated_tasks"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
    parent_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 变量说明：parent_session_id 表示parent_session 对象的唯一标识。
    parent_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：child_run_id 表示child_run 对象的唯一标识。
    child_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    # 变量说明：child_agent_id 表示child_agent 对象的唯一标识。
    child_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
    plan_step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plan_steps.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：teammate_id 表示teammate 对象的唯一标识。
    teammate_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("teammate_workers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 变量说明：status 表示当前对象或运行的状态。
    status: Mapped[str] = mapped_column(String(32), default="in_progress", index=True, nullable=False)
    # 变量说明：idempotency_key 表示当前步骤使用的 idempotency_key 值。
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # 变量说明：result 表示本步骤产生的结果。
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


# 类职责：定义 UsageRecord 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class UsageRecord(TimestampMixin, Base):
    # 变量说明：__tablename__ 表示当前步骤使用的 __tablename__ 值。
    __tablename__ = "usage_records"

    # 变量说明：id 表示当前对象的唯一标识。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 变量说明：run_id 表示当前运行标识。
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    # 变量说明：session_id 表示所属会话标识。
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：agent_id 表示智能体标识。
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("model_connections.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    # 变量说明：provider 表示模型供应商。
    provider: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    # 变量说明：request_count 表示request 的数量。
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：input_tokens 表示当前流程使用的 input_tokens 集合。
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：output_tokens 表示当前流程使用的 output_tokens 集合。
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：cache_creation_tokens 表示当前流程使用的 cache_creation_tokens 集合。
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：cache_read_tokens 表示当前流程使用的 cache_read_tokens 集合。
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：total_tokens 表示当前流程使用的 total_tokens 集合。
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 变量说明：cost_usd 表示当前步骤使用的 cost_usd 值。
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
