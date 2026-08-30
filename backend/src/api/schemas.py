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
WorkflowProfileId = Literal["auto", "general", "coding", "review", "debug"]
PermissionMode = Literal["ask", "smart", "full"]
SkillMarketBrowseView = Literal["all-time", "trending", "hot", "curated"]


class ToolRead(BaseModel):
    id: str
    name: str
    label: str
    description: str
    category: str
    risk_level: str
    enabled: bool
    is_builtin: bool = True
    availability: str
    runtime_tool_id: str | None = None
    requires_approval: bool = False


class SkillRead(ORMModel):
    id: str
    slug: str
    name: str
    description: str
    source: str
    source_url: str | None
    root_path: str
    version: str | None
    enabled: bool
    installed_at: datetime


class SkillImportRequest(BaseModel):
    source_path: str = Field(min_length=1, max_length=4096)


class SkillMarketSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


class SkillMarketItem(BaseModel):
    id: str
    slug: str
    name: str
    source: str
    source_url: str | None = None
    market_url: str | None = None
    installs: int | None = None
    change: int | None = None
    installs_yesterday: int | None = None
    is_official: bool = False
    official_owner: str | None = None
    is_duplicate: bool = False


class SkillMarketSearchRead(BaseModel):
    provider: str = "skills.sh"
    available: bool
    message: str | None = None
    items: list[SkillMarketItem] = Field(default_factory=list)


class SkillMarketBrowseRead(SkillMarketSearchRead):
    """A safe, normalized page from the skills.sh public catalog."""

    view: SkillMarketBrowseView = "all-time"
    page: int = 0
    has_more: bool = False
    total: int | None = None


class SkillMarketLeaderboardCategory(BaseModel):
    """One fixed topical leaderboard, with at most six popular Skills."""

    id: str
    name: str
    description: str
    items: list[SkillMarketItem] = Field(default_factory=list, max_length=6)


class SkillMarketLeaderboardsRead(SkillMarketSearchRead):
    """Cached, topic-based marketplace leaderboards."""

    categories: list[SkillMarketLeaderboardCategory] = Field(default_factory=list)
    refreshed_at: datetime | None = None
    expires_at: datetime | None = None
    ttl_seconds: int
    cached: bool = False


class SkillMarketInstallRequest(BaseModel):
    market_id: str | None = Field(default=None, min_length=3, max_length=300)
    source_url: str | None = Field(default=None, min_length=8, max_length=2048)
    skill_path: str | None = Field(default=None, max_length=512)
    confirm: bool = False

    @model_validator(mode="after")
    def _require_exactly_one_source(self) -> "SkillMarketInstallRequest":
        sources = [item for item in (self.market_id, self.source_url) if item and item.strip()]
        if len(sources) != 1:
            raise ValueError("provide exactly one of market_id or source_url")
        return self


class SkillFilePreview(BaseModel):
    path: str
    size: int


class SkillInstallRead(BaseModel):
    installed: bool
    source_url: str
    candidates: list[str] = Field(default_factory=list)
    files: list[SkillFilePreview] = Field(default_factory=list)
    skill: SkillRead | None = None
    message: str | None = None


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    system_prompt: str = ""
    workspace_id: str | None = None
    model_connection_id: str | None = None
    model_id: str | None = None
    thinking_level: ThinkingLevel = "auto"
    mode: AgentMode = "auto"
    workflow_profile_id: WorkflowProfileId = "auto"
    enabled: bool = True
    tool_ids: list[str] = Field(default_factory=list, max_length=64)
    skill_ids: list[str] = Field(default_factory=list, max_length=128)


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    system_prompt: str | None = None
    workspace_id: str | None = None
    model_connection_id: str | None = None
    model_id: str | None = None
    thinking_level: ThinkingLevel | None = None
    mode: AgentMode | None = None
    workflow_profile_id: WorkflowProfileId | None = None
    enabled: bool | None = None
    tool_ids: list[str] | None = Field(default=None, max_length=64)
    skill_ids: list[str] | None = Field(default=None, max_length=128)


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
    workflow_profile_id: str
    enabled: bool
    is_default: bool
    tool_ids: list[str]
    skill_ids: list[str]
    created_at: datetime
    updated_at: datetime


class SessionCreate(BaseModel):
    title: str = Field(default="New session", min_length=1, max_length=200)
    workspace_id: str | None = None
    agent_id: str | None = None
    model_connection_id: str | None = None
    model_id: str | None = None
    thinking_level: ThinkingLevel = "auto"
    permission_mode: PermissionMode = "smart"
    use_memories: bool = True
    skill_ids: list[str] = Field(default_factory=list, max_length=128)
    mcp_server_names: list[str] = Field(default_factory=list, max_length=128)


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_id: str | None = None
    agent_id: str | None = None
    model_connection_id: str | None = None
    model_id: str | None = None
    thinking_level: ThinkingLevel | None = None
    permission_mode: PermissionMode | None = None
    use_memories: bool | None = None
    skill_ids: list[str] | None = Field(default=None, max_length=128)
    mcp_server_names: list[str] | None = Field(default=None, max_length=128)
    status: str | None = None


class SessionRead(ORMModel):
    id: str
    title: str
    workspace_id: str | None
    agent_id: str | None
    model_connection_id: str | None
    model_id: str | None
    thinking_level: str
    permission_mode: str
    use_memories: bool
    skill_ids: list[str]
    mcp_server_names: list[str]
    context_tokens: int
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
    turn_id: str | None
    message_kind: str
    extra: dict[str, Any] = Field(serialization_alias="metadata")
    created_at: datetime


class RunCreate(BaseModel):
    session_id: str | None = None
    workspace_id: str | None = None
    agent_id: str | None = None
    mode: AgentMode = "auto"
    status: str = "received"
    task_id: str | None = None
    plan_step_id: str | None = None
    run_kind: Literal["initial", "continuation", "recovery"] = "initial"
    resumed_from_run_id: str | None = None


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
    turn_id: str | None
    task_id: str | None
    plan_step_id: str | None
    run_kind: str
    resumed_from_run_id: str | None
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
    session_title: str | None = None
    agent_name: str | None = None


class PlanStepRead(ORMModel):
    id: str
    external_id: str
    position: int
    title: str
    description: str
    status: str
    completed_work: list[Any]
    remaining_work: list[Any]
    next_action: str
    result: str
    evidence: list[Any]
    depends_on: list[str]
    executor_kind: str
    assigned_agent_id: str | None
    assigned_run_id: str | None
    claim_owner: str | None
    workspace_mode: str
    worktree_path: str | None
    attempt: int
    error: str | None
    last_run_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DurableTaskRead(BaseModel):
    id: str
    session_id: str
    origin_turn_id: str | None
    goal: str
    constraints: list[Any]
    status: str
    active_step_id: str | None
    resume_summary: str
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    steps: list[PlanStepRead]


class DraftLaunchRequest(BaseModel):
    """Materialise one unsaved conversation draft and launch its first run.

    ``idempotency_key`` is generated and retained by the client for the life
    of the in-memory draft.  Sending the same key again is a retry, not a new
    conversation.
    """

    idempotency_key: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=100_000)
    root_path: str | None = Field(default=None, max_length=4096)
    model_connection_id: str | None = Field(default=None, max_length=36)
    model_id: str | None = Field(default=None, max_length=255)
    thinking_level: ThinkingLevel = "auto"
    permission_mode: PermissionMode = "smart"
    use_memories: bool = True
    skill_ids: list[str] = Field(default_factory=list, max_length=128)
    mcp_server_names: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("idempotency_key", "title")
    @classmethod
    def _require_non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("content")
    @classmethod
    def _normalize_content(cls, value: str) -> str:
        return value.strip()

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


class MemorySettingsRead(BaseModel):
    enabled: bool


class MemorySettingsUpdate(BaseModel):
    enabled: bool


class MemoryCreate(BaseModel):
    scope: MemoryScope
    scope_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    title: str = Field(default="Memory", min_length=1, max_length=200)
    memory_type: Literal["user", "feedback", "project", "reference"] = "project"
    description: str = ""
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scope_id(self) -> "MemoryCreate":
        if self.scope != "global" and not self.scope_id:
            raise ValueError("scope_id is required for workspace and session memories")
        if self.scope == "global" and self.scope_id:
            raise ValueError("global memories must not include scope_id")
        return self


class MemoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    memory_type: Literal["user", "feedback", "project", "reference"] | None = None
    description: str | None = None
    content: str | None = Field(default=None, min_length=1)
    tags: list[str] | None = None
    pinned: bool | None = None
    status: Literal["active", "superseded", "archived"] | None = None
    metadata: dict[str, Any] | None = None


class MemoryRead(ORMModel):
    id: str
    scope: str
    scope_id: str | None
    name: str
    title: str
    memory_type: str
    description: str
    content: str
    tags: list[str]
    pinned: bool
    status: str
    source_session_id: str | None
    source_turn_id: str | None
    superseded_by: str | None
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


class UsageRunRead(BaseModel):
    run_id: str
    provider: str | None = None
    model_id: str | None = None
    requests: int
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
    cache_hit_rate: float


class UsageSessionRead(BaseModel):
    session_id: str | None
    title: str | None
    requests: int
    tokens: int
    total_cost_usd: float
    avg_cost_usd: float
    cache_hit_rate: float


class UsageWorkspaceRead(BaseModel):
    workspace_id: str | None
    name: str | None
    path: str | None
    requests: int
    tokens: int
    total_cost_usd: float
    avg_cost_usd: float
    cache_hit_rate: float


class DashboardRead(BaseModel):
    workspaces: int
    agents: int
    sessions: int
    active_runs: int
    pending_approvals: int
    model_connections: int


class DelegatedTaskRead(ORMModel):
    """Read-only child-Agent delegation state for a conversation."""

    id: str
    parent_run_id: str
    parent_session_id: str | None
    child_run_id: str | None
    child_agent_id: str | None
    plan_step_id: str | None
    teammate_id: str | None
    title: str
    description: str
    status: str
    result: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class BackgroundJobRead(ORMModel):
    """Durable background command state visible from a conversation."""

    id: str
    run_id: str | None
    session_id: str | None
    workspace_id: str | None
    plan_step_id: str | None
    command: str
    shell: str
    status: str
    timeout_seconds: int
    pid: int | None
    exit_code: int | None
    log_path: str
    output_preview: str
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    observed_at: datetime | None
    observed_by_run_id: str | None
    waiting_run_id: str | None
    created_at: datetime
    updated_at: datetime


class TeammateRead(ORMModel):
    id: str
    team_id: str
    agent_id: str
    name: str
    role: str
    status: str
    current_plan_step_id: str | None
    last_run_id: str | None
    workspace_mode: str
    worktree_path: str | None
    branch_name: str | None
    created_at: datetime
    updated_at: datetime


class CollaborationMessageRead(ORMModel):
    id: str
    team_id: str
    sender_worker_id: str | None
    recipient_worker_id: str | None
    message_type: str
    content: str
    payload: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class CollaborationEventRead(ORMModel):
    id: str
    task_id: str | None
    plan_step_id: str | None
    run_id: str | None
    source_kind: str
    source_id: str
    event_type: str
    payload: dict[str, Any]
    consumed_at: datetime | None
    consumer_run_id: str | None
    created_at: datetime
