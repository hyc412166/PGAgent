"""Pydantic request/response contracts shared by PGAgent APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class WorkspaceCreate(BaseModel):
    # The project picker normally provides only a directory. Keep ``name``
    # optional for that path while accepting the older explicit-name payload.
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str = ""
    root_path: str = Field(min_length=1)
    enabled: bool = True


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    root_path: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class WorkspaceRead(ORMModel):
    id: str
    name: str
    description: str
    root_path: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


ThinkingLevel = Literal["off", "auto", "low", "medium", "high", "xhigh"]
AgentMode = Literal["auto", "direct", "plan"]


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    system_prompt: str = ""
    workspace_id: str | None = None
    model_connection_id: str | None = None
    model_id: str | None = None
    thinking_level: ThinkingLevel = "auto"
    mode: AgentMode = "auto"
    enabled: bool = True


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    system_prompt: str | None = None
    workspace_id: str | None = None
    model_connection_id: str | None = None
    model_id: str | None = None
    thinking_level: ThinkingLevel | None = None
    mode: AgentMode | None = None
    enabled: bool | None = None


class AgentRead(ORMModel):
    id: str
    name: str
    description: str
    system_prompt: str
    workspace_id: str | None
    model_connection_id: str | None
    model_id: str | None
    thinking_level: str
    mode: str
    enabled: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime


class SessionCreate(BaseModel):
    title: str = Field(default="New session", min_length=1, max_length=200)
    workspace_id: str | None = None
    agent_id: str | None = None
    model_connection_id: str | None = None
    model_id: str | None = None
    thinking_level: ThinkingLevel = "auto"
    context_summary: str = ""


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_id: str | None = None
    agent_id: str | None = None
    model_connection_id: str | None = None
    model_id: str | None = None
    thinking_level: ThinkingLevel | None = None
    context_summary: str | None = None
    status: str | None = None


class SessionRead(ORMModel):
    id: str
    title: str
    workspace_id: str | None
    agent_id: str | None
    model_connection_id: str | None
    model_id: str | None
    thinking_level: str
    context_summary: str
    context_tokens: int
    last_compacted_at: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime


class ChatMessageCreate(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_name: str | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatMessageRead(ORMModel):
    id: str
    session_id: str
    role: str
    content: str
    tool_name: str | None
    tool_call_id: str | None
    extra: dict[str, Any] = Field(serialization_alias="metadata")
    created_at: datetime


class RunCreate(BaseModel):
    session_id: str | None = None
    workspace_id: str | None = None
    agent_id: str | None = None
    mode: AgentMode = "auto"
    status: str = "received"


class RunUpdate(BaseModel):
    status: str | None = None
    current_step: int | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    no_progress_steps: int | None = Field(default=None, ge=0)
    stop_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    finished_at: datetime | None = None


class RunRead(ORMModel):
    id: str
    session_id: str | None
    workspace_id: str | None
    agent_id: str | None
    status: str
    mode: str
    current_step: int
    tool_calls: int
    no_progress_steps: int
    stop_reason: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class DraftLaunchRequest(BaseModel):
    """Materialise one unsaved conversation draft and launch its first run.

    ``idempotency_key`` is generated and retained by the client for the life
    of the in-memory draft.  Sending the same key again is a retry, not a new
    conversation.
    """

    idempotency_key: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    root_path: str | None = Field(default=None, max_length=4096)
    model_connection_id: str | None = Field(default=None, max_length=36)
    model_id: str | None = Field(default=None, max_length=255)
    thinking_level: ThinkingLevel = "auto"

    @field_validator("idempotency_key", "title", "content")
    @classmethod
    def _require_non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("root_path", "model_connection_id", "model_id")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class DraftLaunchRead(BaseModel):
    """The persisted resources that now own a previously in-memory draft."""

    workspace: WorkspaceRead | None
    session: SessionRead
    run: RunRead
    reused: bool = False


class RunEventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=80)
    step: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class RunEventRead(ORMModel):
    id: str
    run_id: str
    event_type: str
    step: int | None
    payload: dict[str, Any]
    created_at: datetime


class ApprovalCreate(BaseModel):
    run_id: str
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class ApprovalDecision(BaseModel):
    status: Literal["approved", "rejected"]
    reason: str | None = None


class ApprovalRead(ORMModel):
    id: str
    run_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str
    reason: str | None
    created_at: datetime
    decided_at: datetime | None


MemoryScope = Literal["global", "workspace", "session"]


class MemoryCreate(BaseModel):
    scope: MemoryScope
    scope_id: str | None = None
    title: str = Field(default="Memory", min_length=1, max_length=200)
    content: str = Field(min_length=1)
    pinned: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scope_id(self) -> "MemoryCreate":
        if self.scope != "global" and not self.scope_id:
            raise ValueError("scope_id is required for workspace and session memories")
        return self


class MemoryUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1)
    pinned: bool | None = None
    metadata: dict[str, Any] | None = None


class MemoryRead(ORMModel):
    id: str
    scope: str
    scope_id: str | None
    title: str
    content: str
    pinned: bool
    extra: dict[str, Any] = Field(serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime


class ModelConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    provider: str = "openai_compatible"
    manual_models: list[str] = Field(default_factory=list)
    default_model: str | None = None
    thinking_level: ThinkingLevel = "auto"
    custom_headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("manual_models")
    @classmethod
    def unique_models(cls, models: list[str]) -> list[str]:
        return list(dict.fromkeys(model.strip() for model in models if model.strip()))

    @field_validator("custom_headers")
    @classmethod
    def reject_secret_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        forbidden = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
        if forbidden.intersection(name.lower() for name in headers):
            raise ValueError("Secret-bearing headers must be supplied through api_key, not custom_headers")
        return headers


class ModelConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=1)
    api_key: str | None = Field(default=None, min_length=1)
    provider: str | None = None
    manual_models: list[str] | None = None
    default_model: str | None = None
    thinking_level: ThinkingLevel | None = None
    custom_headers: dict[str, str] | None = None
    enabled: bool | None = None

    @field_validator("manual_models")
    @classmethod
    def unique_models(cls, models: list[str] | None) -> list[str] | None:
        if models is None:
            return None
        return list(dict.fromkeys(model.strip() for model in models if model.strip()))

    @field_validator("custom_headers")
    @classmethod
    def reject_secret_headers(cls, headers: dict[str, str] | None) -> dict[str, str] | None:
        if headers is None:
            return None
        forbidden = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
        if forbidden.intersection(name.lower() for name in headers):
            raise ValueError("Secret-bearing headers must be supplied through api_key, not custom_headers")
        return headers


class ModelConnectionRead(ORMModel):
    id: str
    name: str
    provider: str
    base_url: str
    discovered_models: list[str]
    manual_models: list[str]
    default_model: str | None
    thinking_level: str
    custom_headers: dict[str, str]
    capabilities: dict[str, Any]
    status: str
    last_error: str | None
    last_checked_at: datetime | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ConnectionTestResult(BaseModel):
    success: bool
    category: Literal[
        "ok", "invalid_credentials", "rate_limited", "provider_error", "network_error", "missing_secret"
    ]
    message: str
    retryable: bool
    http_status: int | None = None
    models: list[str] = Field(default_factory=list)


class UsageSummaryRead(BaseModel):
    total_requests: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    total_tokens: int
    total_cost_usd: float
    cache_hit_rate: float


class UsageModelRead(BaseModel):
    provider: str
    model_id: str
    requests: int
    tokens: int
    total_cost_usd: float
    avg_cost_usd: float


class DashboardRead(BaseModel):
    workspaces: int
    agents: int
    sessions: int
    active_runs: int
    pending_approvals: int
    model_connections: int


TaskStatus = Literal["todo", "in_progress", "review", "completed", "blocked"]


class TeamTaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    team_id: str = "default"
    parent_task_id: str | None = None
    assignee_agent_id: str | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    idempotency_key: str | None = None


class TeamTaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: TaskStatus | None = None
    assignee_agent_id: str | None = None
    priority: Literal["low", "normal", "high"] | None = None
    result: dict[str, Any] | None = None
    expected_version: int | None = Field(default=None, ge=1)


class TeamTaskClaim(BaseModel):
    agent_id: str
    lease_owner: str
    expected_version: int = Field(ge=1)
    lease_seconds: int = Field(default=300, ge=10, le=3600)


class TeamTaskRead(ORMModel):
    id: str
    team_id: str
    parent_task_id: str | None
    title: str
    description: str
    priority: str
    status: str
    assignee_agent_id: str | None
    version: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    idempotency_key: str | None
    result: dict[str, Any]
    created_at: datetime
    updated_at: datetime


AgentMessageType = Literal[
    "TASK_ASSIGNED",
    "PLAN_SUBMITTED",
    "PROGRESS",
    "ARTIFACT_READY",
    "BLOCKED",
    "REVIEW_REQUESTED",
    "REVIEW_RESULT",
    "TASK_COMPLETED",
]


class AgentMessageCreate(BaseModel):
    team_id: str = "default"
    task_id: str | None = None
    sender_agent_id: str | None = None
    recipient_agent_id: str | None = None
    message_type: AgentMessageType
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    hop_count: int = Field(default=0, ge=0, le=8)
    expires_at: datetime | None = None


class AgentMessageRead(ORMModel):
    id: str
    team_id: str
    task_id: str | None
    sender_agent_id: str | None
    recipient_agent_id: str | None
    message_type: str
    payload: dict[str, Any]
    status: str
    idempotency_key: str
    hop_count: int
    expires_at: datetime | None
    acknowledged_at: datetime | None
    created_at: datetime
