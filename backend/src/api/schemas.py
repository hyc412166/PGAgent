"""Pydantic request/response contracts shared by PGAgent APIs."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 schemas 子模块。
# 逻辑关系：上层通过 api/schemas.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# 类职责：定义 ORMModel 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ORMModel(BaseModel):
    # 变量说明：model_config 表示当前步骤使用的 model_config 值。
    model_config = ConfigDict(from_attributes=True)


# 类职责：定义 ValidationRuntimeConfig 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ValidationRuntimeConfig(BaseModel):
    # 变量说明：kind 表示当前步骤使用的 kind 值。
    kind: Literal["local", "docker"] = "local"
    # 变量说明：image 表示当前步骤使用的 image 值。
    image: str | None = Field(default=None, max_length=500)

    # 函数职责：校验 runtime 对应的数据或流程。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @model_validator(mode="after")
    def validate_runtime(self) -> "ValidationRuntimeConfig":
        # 变量说明：image 表示当前步骤使用的 image 值。
        image = str(self.image or "").strip()
        if self.kind == "docker" and not image:
            raise ValueError("Docker validation runtime requires an image")
        if image.startswith("-"):
            raise ValueError("Validation image must be an image reference, not a Docker option")
        if image and any(character.isspace() or ord(character) < 32 for character in image):
            raise ValueError("Validation image must not contain whitespace or control characters")
        # 变量说明：image 表示当前步骤使用的 image 值。
        self.image = image or None
        return self


# 类职责：定义 WorkspaceCreate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class WorkspaceCreate(BaseModel):
    # The project picker normally provides only a directory. Keep ``name``
    # optional for that path while accepting the older explicit-name payload.
    # 变量说明：name 表示当前对象名称。
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str = ""
    # 变量说明：root_path 表示root_path 对应的文件系统位置。
    root_path: str = Field(min_length=1)
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool = True
    # 变量说明：validation_runtime 表示当前步骤使用的 validation_runtime 值。
    validation_runtime: ValidationRuntimeConfig = Field(default_factory=ValidationRuntimeConfig)


# 类职责：定义 WorkspaceUpdate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class WorkspaceUpdate(BaseModel):
    # 变量说明：name 表示当前对象名称。
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str | None = None
    # 变量说明：root_path 表示root_path 对应的文件系统位置。
    root_path: str | None = Field(default=None, min_length=1)
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool | None = None
    # 变量说明：validation_runtime 表示当前步骤使用的 validation_runtime 值。
    validation_runtime: ValidationRuntimeConfig | None = None


# 类职责：定义 WorkspaceRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class WorkspaceRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：root_path 表示root_path 对应的文件系统位置。
    root_path: str
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool
    # 变量说明：validation_runtime 表示当前步骤使用的 validation_runtime 值。
    validation_runtime: ValidationRuntimeConfig
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: datetime


# 变量说明：ThinkingLevel 表示当前步骤使用的 ThinkingLevel 值。
ThinkingLevel = Literal["low", "medium", "high", "xhigh"]
# 变量说明：AgentMode 表示当前步骤使用的 AgentMode 值。
AgentMode = Literal["auto", "direct", "plan"]
# 变量说明：WorkflowProfileId 表示当前步骤使用的 WorkflowProfileId 值。
WorkflowProfileId = Literal["auto", "general", "coding", "review", "debug"]
# 变量说明：PermissionMode 表示当前步骤使用的 PermissionMode 值。
PermissionMode = Literal["ask", "smart", "full"]
# 变量说明：SkillMarketBrowseView 表示当前步骤使用的 SkillMarketBrowseView 值。
SkillMarketBrowseView = Literal["all-time", "trending", "hot", "curated"]


# 类职责：定义 ToolRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ToolRead(BaseModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：label 表示当前步骤使用的 label 值。
    label: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：category 表示当前步骤使用的 category 值。
    category: str
    # 变量说明：risk_level 表示当前步骤使用的 risk_level 值。
    risk_level: str
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool
    # 变量说明：is_builtin 表示表示是否满足 builtin 条件的布尔标记。
    is_builtin: bool = True
    # 变量说明：availability 表示当前步骤使用的 availability 值。
    availability: str
    # 变量说明：runtime_tool_id 表示runtime_tool 对象的唯一标识。
    runtime_tool_id: str | None = None
    # 变量说明：requires_approval 表示当前步骤使用的 requires_approval 值。
    requires_approval: bool = False


# 类职责：定义 SkillRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：slug 表示当前步骤使用的 slug 值。
    slug: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：source 表示当前步骤使用的 source 值。
    source: str
    # 变量说明：source_url 表示source 的访问地址。
    source_url: str | None
    # 变量说明：root_path 表示root_path 对应的文件系统位置。
    root_path: str
    # 变量说明：version 表示当前步骤使用的 version 值。
    version: str | None
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool
    # 变量说明：installed_at 表示installed_at 对应的时间信息。
    installed_at: datetime


# 类职责：定义 SkillImportRequest 的跨层数据契约。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillImportRequest(BaseModel):
    # 变量说明：source_path 表示source_path 对应的文件系统位置。
    source_path: str = Field(min_length=1, max_length=4096)


# 类职责：定义 SkillMarketSearchRequest 的跨层数据契约。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillMarketSearchRequest(BaseModel):
    # 变量说明：query 表示当前步骤使用的 query 值。
    query: str = Field(min_length=2, max_length=200)
    # 变量说明：limit 表示当前步骤使用的 limit 值。
    limit: int = Field(default=20, ge=1, le=100)


# 类职责：定义 SkillMarketItem 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillMarketItem(BaseModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：slug 表示当前步骤使用的 slug 值。
    slug: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：source 表示当前步骤使用的 source 值。
    source: str
    # 变量说明：source_url 表示source 的访问地址。
    source_url: str | None = None
    # 变量说明：market_url 表示market 的访问地址。
    market_url: str | None = None
    # 变量说明：installs 表示当前流程使用的 installs 集合。
    installs: int | None = None
    # 变量说明：change 表示当前步骤使用的 change 值。
    change: int | None = None
    # 变量说明：installs_yesterday 表示当前步骤使用的 installs_yesterday 值。
    installs_yesterday: int | None = None
    # 变量说明：is_official 表示表示是否满足 official 条件的布尔标记。
    is_official: bool = False
    # 变量说明：official_owner 表示当前步骤使用的 official_owner 值。
    official_owner: str | None = None
    # 变量说明：is_duplicate 表示表示是否满足 duplicate 条件的布尔标记。
    is_duplicate: bool = False


# 类职责：定义 SkillMarketSearchRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillMarketSearchRead(BaseModel):
    # 变量说明：provider 表示模型供应商。
    provider: str = "skills.sh"
    # 变量说明：available 表示当前步骤使用的 available 值。
    available: bool
    # 变量说明：message 表示当前消息。
    message: str | None = None
    # 变量说明：items 表示待处理的元素集合。
    items: list[SkillMarketItem] = Field(default_factory=list)


# 类职责：定义 SkillMarketBrowseRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillMarketBrowseRead(SkillMarketSearchRead):
    """A safe, normalized page from the skills.sh public catalog."""

    # 变量说明：view 表示当前步骤使用的 view 值。
    view: SkillMarketBrowseView = "all-time"
    # 变量说明：page 表示当前步骤使用的 page 值。
    page: int = 0
    # 变量说明：has_more 表示表示是否满足 _more 条件的布尔标记。
    has_more: bool = False
    # 变量说明：total 表示当前步骤使用的 total 值。
    total: int | None = None


# 类职责：定义 SkillMarketLeaderboardCategory 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillMarketLeaderboardCategory(BaseModel):
    """One fixed topical leaderboard, with at most six popular Skills."""

    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：items 表示待处理的元素集合。
    items: list[SkillMarketItem] = Field(default_factory=list, max_length=6)


# 类职责：定义 SkillMarketLeaderboardsRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillMarketLeaderboardsRead(SkillMarketSearchRead):
    """Cached, topic-based marketplace leaderboards."""

    # 变量说明：categories 表示当前流程使用的 categories 集合。
    categories: list[SkillMarketLeaderboardCategory] = Field(default_factory=list)
    # 变量说明：refreshed_at 表示refreshed_at 对应的时间信息。
    refreshed_at: datetime | None = None
    # 变量说明：expires_at 表示expires_at 对应的时间信息。
    expires_at: datetime | None = None
    # 变量说明：ttl_seconds 表示当前流程使用的 ttl_seconds 集合。
    ttl_seconds: int
    # 变量说明：cached 表示当前步骤使用的 cached 值。
    cached: bool = False


# 类职责：定义 SkillMarketInstallRequest 的跨层数据契约。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillMarketInstallRequest(BaseModel):
    # 变量说明：market_id 表示market 对象的唯一标识。
    market_id: str | None = Field(default=None, min_length=3, max_length=300)
    # 变量说明：source_url 表示source 的访问地址。
    source_url: str | None = Field(default=None, min_length=8, max_length=2048)
    # 变量说明：skill_path 表示skill_path 对应的文件系统位置。
    skill_path: str | None = Field(default=None, max_length=512)
    # 变量说明：confirm 表示当前步骤使用的 confirm 值。
    confirm: bool = False

    # 函数职责：完成 require_exactly_one_source 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @model_validator(mode="after")
    def _require_exactly_one_source(self) -> "SkillMarketInstallRequest":
        # 变量说明：sources 表示当前流程使用的 sources 集合。
        sources = [item for item in (self.market_id, self.source_url) if item and item.strip()]
        if len(sources) != 1:
            raise ValueError("provide exactly one of market_id or source_url")
        return self


# 类职责：定义 SkillFilePreview 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillFilePreview(BaseModel):
    # 变量说明：path 表示当前文件或目录路径。
    path: str
    # 变量说明：size 表示当前步骤使用的 size 值。
    size: int


# 类职责：定义 SkillInstallRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SkillInstallRead(BaseModel):
    # 变量说明：installed 表示当前步骤使用的 installed 值。
    installed: bool
    # 变量说明：source_url 表示source 的访问地址。
    source_url: str
    # 变量说明：candidates 表示当前流程使用的 candidates 集合。
    candidates: list[str] = Field(default_factory=list)
    # 变量说明：files 表示当前流程使用的 files 集合。
    files: list[SkillFilePreview] = Field(default_factory=list)
    # 变量说明：skill 表示当前步骤使用的 skill 值。
    skill: SkillRead | None = None
    # 变量说明：message 表示当前消息。
    message: str | None = None


# 类职责：定义 AgentCreate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class AgentCreate(BaseModel):
    # 变量说明：name 表示当前对象名称。
    name: str = Field(min_length=1, max_length=120)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str = ""
    # 变量说明：system_prompt 表示当前步骤使用的 system_prompt 值。
    system_prompt: str = ""
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: str | None = None
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: str | None = None
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: str | None = None
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: ThinkingLevel = "medium"
    # 变量说明：mode 表示当前步骤使用的 mode 值。
    mode: AgentMode = "auto"
    # 变量说明：workflow_profile_id 表示workflow_profile 对象的唯一标识。
    workflow_profile_id: WorkflowProfileId = "auto"
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool = True
    # 变量说明：tool_ids 表示tool 对象标识集合。
    tool_ids: list[str] = Field(default_factory=list, max_length=64)
    # 变量说明：skill_ids 表示skill 对象标识集合。
    skill_ids: list[str] = Field(default_factory=list, max_length=128)


# 类职责：定义 AgentUpdate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class AgentUpdate(BaseModel):
    # 变量说明：name 表示当前对象名称。
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str | None = None
    # 变量说明：system_prompt 表示当前步骤使用的 system_prompt 值。
    system_prompt: str | None = None
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: str | None = None
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: str | None = None
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: str | None = None
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: ThinkingLevel | None = None
    # 变量说明：mode 表示当前步骤使用的 mode 值。
    mode: AgentMode | None = None
    # 变量说明：workflow_profile_id 表示workflow_profile 对象的唯一标识。
    workflow_profile_id: WorkflowProfileId | None = None
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool | None = None
    # 变量说明：tool_ids 表示tool 对象标识集合。
    tool_ids: list[str] | None = Field(default=None, max_length=64)
    # 变量说明：skill_ids 表示skill 对象标识集合。
    skill_ids: list[str] | None = Field(default=None, max_length=128)


# 类职责：定义 AgentRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class AgentRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：system_prompt 表示当前步骤使用的 system_prompt 值。
    system_prompt: str
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: str | None
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: str | None
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: str | None
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: str
    # 变量说明：mode 表示当前步骤使用的 mode 值。
    mode: str
    # 变量说明：workflow_profile_id 表示workflow_profile 对象的唯一标识。
    workflow_profile_id: str
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool
    # 变量说明：is_default 表示表示是否满足 default 条件的布尔标记。
    is_default: bool
    # 变量说明：tool_ids 表示tool 对象标识集合。
    tool_ids: list[str]
    # 变量说明：skill_ids 表示skill 对象标识集合。
    skill_ids: list[str]
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: datetime


# 类职责：定义 SessionCreate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SessionCreate(BaseModel):
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str = Field(default="New session", min_length=1, max_length=200)
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: str | None = None
    # 变量说明：agent_id 表示智能体标识。
    agent_id: str | None = None
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: str | None = None
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: str | None = None
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: ThinkingLevel = "medium"
    # 变量说明：permission_mode 表示当前步骤使用的 permission_mode 值。
    permission_mode: PermissionMode = "smart"
    # 变量说明：use_memories 表示当前流程使用的 use_memories 集合。
    use_memories: bool = True
    # 变量说明：skill_ids 表示skill 对象标识集合。
    skill_ids: list[str] = Field(default_factory=list, max_length=128)
    # 变量说明：mcp_server_names 表示当前流程使用的 mcp_server_names 集合。
    mcp_server_names: list[str] = Field(default_factory=list, max_length=128)


# 类职责：定义 SessionUpdate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SessionUpdate(BaseModel):
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str | None = Field(default=None, min_length=1, max_length=200)
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: str | None = None
    # 变量说明：agent_id 表示智能体标识。
    agent_id: str | None = None
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: str | None = None
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: str | None = None
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: ThinkingLevel | None = None
    # 变量说明：permission_mode 表示当前步骤使用的 permission_mode 值。
    permission_mode: PermissionMode | None = None
    # 变量说明：use_memories 表示当前流程使用的 use_memories 集合。
    use_memories: bool | None = None
    # 变量说明：skill_ids 表示skill 对象标识集合。
    skill_ids: list[str] | None = Field(default=None, max_length=128)
    # 变量说明：mcp_server_names 表示当前流程使用的 mcp_server_names 集合。
    mcp_server_names: list[str] | None = Field(default=None, max_length=128)
    # 变量说明：status 表示当前对象或运行的状态。
    status: str | None = None


# 类职责：定义 SessionRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SessionRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: str | None
    # 变量说明：agent_id 表示智能体标识。
    agent_id: str | None
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: str | None
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: str | None
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: str
    # 变量说明：permission_mode 表示当前步骤使用的 permission_mode 值。
    permission_mode: str
    # 变量说明：use_memories 表示当前流程使用的 use_memories 集合。
    use_memories: bool
    # 变量说明：skill_ids 表示skill 对象标识集合。
    skill_ids: list[str]
    # 变量说明：mcp_server_names 表示当前流程使用的 mcp_server_names 集合。
    mcp_server_names: list[str]
    # 变量说明：context_tokens 表示当前流程使用的 context_tokens 集合。
    context_tokens: int
    # 变量说明：status 表示当前对象或运行的状态。
    status: str
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: datetime


# 类职责：定义 ChatMessageCreate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ChatMessageCreate(BaseModel):
    # 变量说明：role 表示当前步骤使用的 role 值。
    role: Literal["system", "user", "assistant", "tool"]
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str = ""
    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    tool_name: str | None = None
    # 变量说明：tool_call_id 表示tool_call 对象的唯一标识。
    tool_call_id: str | None = None
    # 变量说明：metadata 表示当前步骤使用的 metadata 值。
    metadata: dict[str, Any] = Field(default_factory=dict)


# 类职责：定义 MessageCitation 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MessageCitation(BaseModel):
    # 变量说明：url 表示当前步骤使用的 url 值。
    url: str
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str


# 类职责：定义 ChatMessageRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ChatMessageRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：session_id 表示所属会话标识。
    session_id: str
    # 变量说明：role 表示当前步骤使用的 role 值。
    role: str
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str
    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    tool_name: str | None
    # 变量说明：tool_call_id 表示tool_call 对象的唯一标识。
    tool_call_id: str | None
    # 变量说明：turn_id 表示turn 对象的唯一标识。
    turn_id: str | None
    # 变量说明：message_kind 表示当前步骤使用的 message_kind 值。
    message_kind: str
    # 变量说明：extra 表示当前步骤使用的 extra 值。
    extra: dict[str, Any] = Field(serialization_alias="metadata")
    # 变量说明：citations 表示当前流程使用的 citations 集合。
    citations: list[MessageCitation] = Field(default_factory=list)
    # 变量说明：created_at 表示创建时间。
    created_at: datetime


# 类职责：定义 RunCreate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class RunCreate(BaseModel):
    # 变量说明：session_id 表示所属会话标识。
    session_id: str | None = None
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: str | None = None
    # 变量说明：agent_id 表示智能体标识。
    agent_id: str | None = None
    # 变量说明：mode 表示当前步骤使用的 mode 值。
    mode: AgentMode = "auto"
    # 变量说明：status 表示当前对象或运行的状态。
    status: str = "received"
    # 变量说明：task_id 表示任务标识。
    task_id: str | None = None
    # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
    plan_step_id: str | None = None
    # 变量说明：run_kind 表示当前步骤使用的 run_kind 值。
    run_kind: Literal["initial", "continuation", "recovery"] = "initial"
    # 变量说明：resumed_from_run_id 表示resumed_from_run 对象的唯一标识。
    resumed_from_run_id: str | None = None


# 类职责：定义 RunUpdate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class RunUpdate(BaseModel):
    # 变量说明：status 表示当前对象或运行的状态。
    status: str | None = None
    # 变量说明：current_step 表示当前步骤使用的 current_step 值。
    current_step: int | None = Field(default=None, ge=0)
    # 变量说明：tool_calls 表示当前流程使用的 tool_calls 集合。
    tool_calls: int | None = Field(default=None, ge=0)
    # 变量说明：no_progress_steps 表示当前流程使用的 no_progress_steps 集合。
    no_progress_steps: int | None = Field(default=None, ge=0)
    # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
    stop_reason: str | None = None
    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
    error_code: str | None = None
    # 变量说明：error_message 表示当前步骤使用的 error_message 值。
    error_message: str | None = None
    # 变量说明：finished_at 表示finished_at 对应的时间信息。
    finished_at: datetime | None = None


# 类职责：定义 RunRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class RunRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：session_id 表示所属会话标识。
    session_id: str | None
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: str | None
    # 变量说明：agent_id 表示智能体标识。
    agent_id: str | None
    # 变量说明：turn_id 表示turn 对象的唯一标识。
    turn_id: str | None
    # 变量说明：task_id 表示任务标识。
    task_id: str | None
    # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
    plan_step_id: str | None
    # 变量说明：run_kind 表示当前步骤使用的 run_kind 值。
    run_kind: str
    # 变量说明：resumed_from_run_id 表示resumed_from_run 对象的唯一标识。
    resumed_from_run_id: str | None
    # 变量说明：status 表示当前对象或运行的状态。
    status: str
    # 变量说明：mode 表示当前步骤使用的 mode 值。
    mode: str
    # 变量说明：current_step 表示当前步骤使用的 current_step 值。
    current_step: int
    # 变量说明：tool_calls 表示当前流程使用的 tool_calls 集合。
    tool_calls: int
    # 变量说明：no_progress_steps 表示当前流程使用的 no_progress_steps 集合。
    no_progress_steps: int
    # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
    stop_reason: str | None
    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
    error_code: str | None
    # 变量说明：error_message 表示当前步骤使用的 error_message 值。
    error_message: str | None
    # 变量说明：started_at 表示started_at 对应的时间信息。
    started_at: datetime
    # 变量说明：finished_at 表示finished_at 对应的时间信息。
    finished_at: datetime | None
    # 变量说明：session_title 表示当前步骤使用的 session_title 值。
    session_title: str | None = None
    # 变量说明：agent_name 表示当前步骤使用的 agent_name 值。
    agent_name: str | None = None


# 类职责：定义 PlanStepRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class PlanStepRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：external_id 表示external 对象的唯一标识。
    external_id: str
    # 变量说明：position 表示当前步骤使用的 position 值。
    position: int
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：status 表示当前对象或运行的状态。
    status: str
    # 变量说明：completed_work 表示当前步骤使用的 completed_work 值。
    completed_work: list[Any]
    # 变量说明：remaining_work 表示当前步骤使用的 remaining_work 值。
    remaining_work: list[Any]
    # 变量说明：next_action 表示当前步骤使用的 next_action 值。
    next_action: str
    # 变量说明：result 表示本步骤产生的结果。
    result: str
    # 变量说明：evidence 表示当前步骤使用的 evidence 值。
    evidence: list[Any]
    # 变量说明：depends_on 表示当前步骤使用的 depends_on 值。
    depends_on: list[str]
    # 变量说明：executor_kind 表示当前步骤使用的 executor_kind 值。
    executor_kind: str
    # 变量说明：assigned_agent_id 表示assigned_agent 对象的唯一标识。
    assigned_agent_id: str | None
    # 变量说明：assigned_run_id 表示assigned_run 对象的唯一标识。
    assigned_run_id: str | None
    # 变量说明：claim_owner 表示当前步骤使用的 claim_owner 值。
    claim_owner: str | None
    # 变量说明：workspace_mode 表示当前步骤使用的 workspace_mode 值。
    workspace_mode: str
    # 变量说明：worktree_path 表示worktree_path 对应的文件系统位置。
    worktree_path: str | None
    # 变量说明：attempt 表示当前步骤使用的 attempt 值。
    attempt: int
    # 变量说明：error 表示当前捕获或准备上报的错误。
    error: str | None
    # 变量说明：last_run_id 表示last_run 对象的唯一标识。
    last_run_id: str | None
    # 变量说明：started_at 表示started_at 对应的时间信息。
    started_at: datetime | None
    # 变量说明：completed_at 表示completed_at 对应的时间信息。
    completed_at: datetime | None
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: datetime


# 类职责：定义 DurableTaskRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class DurableTaskRead(BaseModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：session_id 表示所属会话标识。
    session_id: str
    # 变量说明：origin_turn_id 表示origin_turn 对象的唯一标识。
    origin_turn_id: str | None
    # 变量说明：goal 表示当前步骤使用的 goal 值。
    goal: str
    # 变量说明：constraints 表示当前流程使用的 constraints 集合。
    constraints: list[Any]
    # 变量说明：status 表示当前对象或运行的状态。
    status: str
    # 变量说明：active_step_id 表示active_step 对象的唯一标识。
    active_step_id: str | None
    # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
    resume_summary: str
    # 变量说明：completed_at 表示completed_at 对应的时间信息。
    completed_at: datetime | None
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: datetime
    # 变量说明：steps 表示当前流程使用的 steps 集合。
    steps: list[PlanStepRead]


# 类职责：定义 DraftLaunchRequest 的跨层数据契约。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class DraftLaunchRequest(BaseModel):
    """Materialise one unsaved conversation draft and launch its first run.

    ``idempotency_key`` is generated and retained by the client for the life
    of the in-memory draft.  Sending the same key again is a retry, not a new
    conversation.
    """

    # 变量说明：idempotency_key 表示当前步骤使用的 idempotency_key 值。
    idempotency_key: str = Field(min_length=1, max_length=255)
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str = Field(min_length=1, max_length=200)
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str = Field(default="", max_length=100_000)
    # 变量说明：root_path 表示root_path 对应的文件系统位置。
    root_path: str | None = Field(default=None, max_length=4096)
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: str | None = Field(default=None, max_length=36)
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: str | None = Field(default=None, max_length=255)
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: ThinkingLevel = "medium"
    # 变量说明：permission_mode 表示当前步骤使用的 permission_mode 值。
    permission_mode: PermissionMode = "smart"
    # 变量说明：use_memories 表示当前流程使用的 use_memories 集合。
    use_memories: bool = True
    # 变量说明：skill_ids 表示skill 对象标识集合。
    skill_ids: list[str] = Field(default_factory=list, max_length=128)
    # 变量说明：mcp_server_names 表示当前流程使用的 mcp_server_names 集合。
    mcp_server_names: list[str] = Field(default_factory=list, max_length=128)

    # 函数职责：完成 require_non_blank_text 对应的业务处理。
    # 参数关系：value 表示当前字段或计算值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @field_validator("idempotency_key", "title")
    @classmethod
    def _require_non_blank_text(cls, value: str) -> str:
        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    # 函数职责：规范化 content 对应的数据或流程。
    # 参数关系：value 表示当前字段或计算值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @field_validator("content")
    @classmethod
    def _normalize_content(cls, value: str) -> str:
        return value.strip()

    # 函数职责：规范化 optional_text 对应的数据或流程。
    # 参数关系：value 表示当前字段或计算值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @field_validator("root_path", "model_connection_id", "model_id")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized = value.strip()
        return normalized or None


# 类职责：定义 DraftLaunchRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class DraftLaunchRead(BaseModel):
    """The persisted resources that now own a previously in-memory draft."""

    # 变量说明：workspace 表示当前步骤使用的 workspace 值。
    workspace: WorkspaceRead | None
    # 变量说明：session 表示当前步骤使用的 session 值。
    session: SessionRead
    # 变量说明：run 表示当前步骤使用的 run 值。
    run: RunRead
    # 变量说明：reused 表示当前步骤使用的 reused 值。
    reused: bool = False


# 类职责：定义 RunEventCreate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class RunEventCreate(BaseModel):
    # 变量说明：event_type 表示当前步骤使用的 event_type 值。
    event_type: str = Field(min_length=1, max_length=80)
    # 变量说明：trace_id 用于关联请求、运行和持久事件。
    trace_id: str | None = Field(default=None, max_length=36)
    # 变量说明：step 表示当前步骤使用的 step 值。
    step: int | None = Field(default=None, ge=0)
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload: dict[str, Any] = Field(default_factory=dict)


# 类职责：定义 RunEventRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class RunEventRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：run_id 表示当前运行标识。
    run_id: str
    # 变量说明：event_type 表示当前步骤使用的 event_type 值。
    event_type: str
    # 变量说明：trace_id 用于关联请求、运行和持久事件。
    trace_id: str | None = None
    # 变量说明：sequence 表示事件在所属 Run 内的持久顺序。
    sequence: int = 0
    # 变量说明：step 表示当前步骤使用的 step 值。
    step: int | None
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload: dict[str, Any]
    # 变量说明：created_at 表示创建时间。
    created_at: datetime


class RunEventPage(BaseModel):
    items: list[RunEventRead]
    next_before: int | None = None


# 类职责：定义 ApprovalCreate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ApprovalCreate(BaseModel):
    # 变量说明：run_id 表示当前运行标识。
    run_id: str
    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    tool_name: str = Field(min_length=1, max_length=100)
    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments: dict[str, Any] = Field(default_factory=dict)
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str | None = None


# 类职责：定义 ApprovalDecision 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ApprovalDecision(BaseModel):
    # 变量说明：status 表示当前对象或运行的状态。
    status: Literal["approved", "rejected"]
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str | None = None


# 类职责：定义 ApprovalRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ApprovalRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：run_id 表示当前运行标识。
    run_id: str
    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    tool_name: str
    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments: dict[str, Any]
    # 变量说明：status 表示当前对象或运行的状态。
    status: str
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str | None
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：decided_at 表示decided_at 对应的时间信息。
    decided_at: datetime | None


# 变量说明：MemoryScope 表示当前步骤使用的 MemoryScope 值。
MemoryScope = Literal["global", "workspace", "session"]


# 类职责：定义 MemorySettingsRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MemorySettingsRead(BaseModel):
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool


# 类职责：定义 MemorySettingsUpdate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MemorySettingsUpdate(BaseModel):
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool


# 类职责：定义 MemoryCreate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MemoryCreate(BaseModel):
    # 变量说明：scope 表示当前步骤使用的 scope 值。
    scope: MemoryScope
    # 变量说明：scope_id 表示scope 对象的唯一标识。
    scope_id: str | None = None
    # 变量说明：name 表示当前对象名称。
    name: str | None = Field(default=None, min_length=1, max_length=200)
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str = Field(default="Memory", min_length=1, max_length=200)
    # 变量说明：memory_type 表示当前步骤使用的 memory_type 值。
    memory_type: Literal["user", "feedback", "project", "reference"] = "project"
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str = ""
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str = Field(min_length=1)
    # 变量说明：tags 表示当前流程使用的 tags 集合。
    tags: list[str] = Field(default_factory=list)
    # 变量说明：pinned 表示当前步骤使用的 pinned 值。
    pinned: bool = False
    # 变量说明：metadata 表示当前步骤使用的 metadata 值。
    metadata: dict[str, Any] = Field(default_factory=dict)

    # 函数职责：校验 scope_id 对应的数据或流程。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @model_validator(mode="after")
    def validate_scope_id(self) -> "MemoryCreate":
        if self.scope != "global" and not self.scope_id:
            raise ValueError("scope_id is required for workspace and session memories")
        if self.scope == "global" and self.scope_id:
            raise ValueError("global memories must not include scope_id")
        return self


# 类职责：定义 MemoryUpdate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MemoryUpdate(BaseModel):
    # 变量说明：name 表示当前对象名称。
    name: str | None = Field(default=None, min_length=1, max_length=200)
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str | None = Field(default=None, min_length=1, max_length=200)
    # 变量说明：memory_type 表示当前步骤使用的 memory_type 值。
    memory_type: Literal["user", "feedback", "project", "reference"] | None = None
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str | None = None
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str | None = Field(default=None, min_length=1)
    # 变量说明：tags 表示当前流程使用的 tags 集合。
    tags: list[str] | None = None
    # 变量说明：pinned 表示当前步骤使用的 pinned 值。
    pinned: bool | None = None
    # 变量说明：status 表示当前对象或运行的状态。
    status: Literal["active", "superseded", "archived"] | None = None
    # 变量说明：metadata 表示当前步骤使用的 metadata 值。
    metadata: dict[str, Any] | None = None


# 类职责：定义 MemoryRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class MemoryRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：scope 表示当前步骤使用的 scope 值。
    scope: str
    # 变量说明：scope_id 表示scope 对象的唯一标识。
    scope_id: str | None
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str
    # 变量说明：memory_type 表示当前步骤使用的 memory_type 值。
    memory_type: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str
    # 变量说明：tags 表示当前流程使用的 tags 集合。
    tags: list[str]
    # 变量说明：pinned 表示当前步骤使用的 pinned 值。
    pinned: bool
    # 变量说明：status 表示当前对象或运行的状态。
    status: str
    # 变量说明：source_session_id 表示source_session 对象的唯一标识。
    source_session_id: str | None
    # 变量说明：source_turn_id 表示source_turn 对象的唯一标识。
    source_turn_id: str | None
    # 变量说明：superseded_by 表示当前步骤使用的 superseded_by 值。
    superseded_by: str | None
    # 变量说明：extra 表示当前步骤使用的 extra 值。
    extra: dict[str, Any] = Field(serialization_alias="metadata")
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: datetime


# 类职责：定义 ModelConnectionCreate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ModelConnectionCreate(BaseModel):
    # 变量说明：name 表示当前对象名称。
    name: str = Field(min_length=1, max_length=120)
    # 变量说明：base_url 表示base 的访问地址。
    base_url: str = Field(min_length=1)
    # 变量说明：api_key 表示当前步骤使用的 api_key 值。
    api_key: str = Field(min_length=1)
    # 变量说明：provider 表示模型供应商。
    provider: str = "openai_compatible"
    # 变量说明：api_protocol 表示当前步骤使用的 api_protocol 值。
    api_protocol: Literal["responses", "chat_completions"] = "responses"
    # 变量说明：manual_models 表示当前流程使用的 manual_models 集合。
    manual_models: list[str] = Field(default_factory=list)
    # disabled_models 记录用户在模型目录中明确停用的模型 ID。
    disabled_models: list[str] = Field(default_factory=list)
    # 变量说明：default_model 表示当前步骤使用的 default_model 值。
    default_model: str | None = None
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: ThinkingLevel = "medium"
    # 变量说明：custom_headers 表示当前流程使用的 custom_headers 集合。
    custom_headers: dict[str, str] = Field(default_factory=dict)
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool = True

    # 函数职责：完成 unique_models 对应的业务处理。
    # 参数关系：models 表示当前流程使用的 models 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @field_validator("manual_models", "disabled_models")
    @classmethod
    def unique_models(cls, models: list[str]) -> list[str]:
        return list(dict.fromkeys(model.strip() for model in models if model.strip()))

    # 函数职责：完成 reject_secret_headers 对应的业务处理。
    # 参数关系：headers 表示当前流程使用的 headers 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @field_validator("custom_headers")
    @classmethod
    def reject_secret_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        # 变量说明：forbidden 表示当前步骤使用的 forbidden 值。
        forbidden = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
        if forbidden.intersection(name.lower() for name in headers):
            raise ValueError("Secret-bearing headers must be supplied through api_key, not custom_headers")
        return headers


# 类职责：定义 ModelConnectionUpdate 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ModelConnectionUpdate(BaseModel):
    # 变量说明：name 表示当前对象名称。
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # 变量说明：base_url 表示base 的访问地址。
    base_url: str | None = Field(default=None, min_length=1)
    # 变量说明：api_key 表示当前步骤使用的 api_key 值。
    api_key: str | None = Field(default=None, min_length=1)
    # 变量说明：provider 表示模型供应商。
    provider: str | None = None
    # 变量说明：api_protocol 表示当前步骤使用的 api_protocol 值。
    api_protocol: Literal["responses", "chat_completions"] | None = None
    # 变量说明：manual_models 表示当前流程使用的 manual_models 集合。
    manual_models: list[str] | None = None
    # disabled_models 由模型标签的启停操作整体提交。
    disabled_models: list[str] | None = None
    # 变量说明：default_model 表示当前步骤使用的 default_model 值。
    default_model: str | None = None
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: ThinkingLevel | None = None
    # 变量说明：custom_headers 表示当前流程使用的 custom_headers 集合。
    custom_headers: dict[str, str] | None = None
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool | None = None

    # 函数职责：完成 unique_models 对应的业务处理。
    # 参数关系：models 表示当前流程使用的 models 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @field_validator("manual_models", "disabled_models")
    @classmethod
    def unique_models(cls, models: list[str] | None) -> list[str] | None:
        if models is None:
            return None
        return list(dict.fromkeys(model.strip() for model in models if model.strip()))

    # 函数职责：完成 reject_secret_headers 对应的业务处理。
    # 参数关系：headers 表示当前流程使用的 headers 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @field_validator("custom_headers")
    @classmethod
    def reject_secret_headers(cls, headers: dict[str, str] | None) -> dict[str, str] | None:
        if headers is None:
            return None
        # 变量说明：forbidden 表示当前步骤使用的 forbidden 值。
        forbidden = {"authorization", "x-api-key", "api-key", "proxy-authorization"}
        if forbidden.intersection(name.lower() for name in headers):
            raise ValueError("Secret-bearing headers must be supplied through api_key, not custom_headers")
        return headers


# 类职责：定义 ModelConnectionRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ModelConnectionRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：provider 表示模型供应商。
    provider: str
    # 变量说明：api_protocol 表示当前步骤使用的 api_protocol 值。
    api_protocol: str
    # 变量说明：base_url 表示base 的访问地址。
    base_url: str
    # 变量说明：discovered_models 表示当前流程使用的 discovered_models 集合。
    discovered_models: list[str]
    # 变量说明：manual_models 表示当前流程使用的 manual_models 集合。
    manual_models: list[str]
    # 变量说明：disabled_models 表示当前连接中不可供会话选择的模型集合。
    disabled_models: list[str]
    # 变量说明：default_model 表示当前步骤使用的 default_model 值。
    default_model: str | None
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: str
    # 变量说明：custom_headers 表示当前流程使用的 custom_headers 集合。
    custom_headers: dict[str, str]
    # 变量说明：capabilities 表示当前流程使用的 capabilities 集合。
    capabilities: dict[str, Any]
    # 变量说明：status 表示当前对象或运行的状态。
    status: str
    # 变量说明：last_error 表示当前步骤使用的 last_error 值。
    last_error: str | None
    # 变量说明：last_checked_at 表示last_checked_at 对应的时间信息。
    last_checked_at: datetime | None
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: datetime


# 类职责：定义 ConnectionTestResult 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ConnectionTestResult(BaseModel):
    # 变量说明：success 表示当前流程使用的 success 集合。
    success: bool
    # 变量说明：category 表示当前步骤使用的 category 值。
    category: Literal[
        "ok", "invalid_credentials", "rate_limited", "provider_error", "network_error", "missing_secret"
    ]
    # 变量说明：message 表示当前消息。
    message: str
    # 变量说明：retryable 表示当前步骤使用的 retryable 值。
    retryable: bool
    # 变量说明：http_status 表示当前流程使用的 http_status 集合。
    http_status: int | None = None
    # 变量说明：models 表示当前流程使用的 models 集合。
    models: list[str] = Field(default_factory=list)


# 类职责：定义 UsageSummaryRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class UsageSummaryRead(BaseModel):
    # 变量说明：total_requests 表示当前流程使用的 total_requests 集合。
    total_requests: int
    # 变量说明：input_tokens 表示当前流程使用的 input_tokens 集合。
    input_tokens: int
    # 变量说明：output_tokens 表示当前流程使用的 output_tokens 集合。
    output_tokens: int
    # 变量说明：cache_creation_tokens 表示当前流程使用的 cache_creation_tokens 集合。
    cache_creation_tokens: int
    # 变量说明：cache_read_tokens 表示当前流程使用的 cache_read_tokens 集合。
    cache_read_tokens: int
    # 变量说明：total_tokens 表示当前流程使用的 total_tokens 集合。
    total_tokens: int
    # 变量说明：total_cost_usd 表示当前步骤使用的 total_cost_usd 值。
    total_cost_usd: float
    # 变量说明：cache_hit_rate 表示当前步骤使用的 cache_hit_rate 值。
    cache_hit_rate: float


# 类职责：定义 UsageRunRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class UsageRunRead(BaseModel):
    # 变量说明：run_id 表示当前运行标识。
    run_id: str
    # 变量说明：provider 表示模型供应商。
    provider: str | None = None
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: str | None = None
    # 变量说明：requests 表示当前流程使用的 requests 集合。
    requests: int
    # 变量说明：input_tokens 表示当前流程使用的 input_tokens 集合。
    input_tokens: int
    # 变量说明：output_tokens 表示当前流程使用的 output_tokens 集合。
    output_tokens: int
    # 变量说明：cache_creation_tokens 表示当前流程使用的 cache_creation_tokens 集合。
    cache_creation_tokens: int
    # 变量说明：cache_read_tokens 表示当前流程使用的 cache_read_tokens 集合。
    cache_read_tokens: int
    # 变量说明：total_tokens 表示当前流程使用的 total_tokens 集合。
    total_tokens: int
    # 变量说明：total_cost_usd 表示当前步骤使用的 total_cost_usd 值。
    total_cost_usd: float
    # 变量说明：cache_hit_rate 表示当前步骤使用的 cache_hit_rate 值。
    cache_hit_rate: float


# 类职责：定义 UsageModelRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class UsageModelRead(BaseModel):
    # 变量说明：provider 表示模型供应商。
    provider: str
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: str
    # 变量说明：requests 表示当前流程使用的 requests 集合。
    requests: int
    # 变量说明：tokens 表示当前流程使用的 tokens 集合。
    tokens: int
    # 变量说明：total_cost_usd 表示当前步骤使用的 total_cost_usd 值。
    total_cost_usd: float
    # 变量说明：avg_cost_usd 表示当前步骤使用的 avg_cost_usd 值。
    avg_cost_usd: float
    # 变量说明：cache_hit_rate 表示当前步骤使用的 cache_hit_rate 值。
    cache_hit_rate: float


# 类职责：定义 UsageSessionRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class UsageSessionRead(BaseModel):
    # 变量说明：session_id 表示所属会话标识。
    session_id: str | None
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str | None
    # 变量说明：requests 表示当前流程使用的 requests 集合。
    requests: int
    # 变量说明：tokens 表示当前流程使用的 tokens 集合。
    tokens: int
    # 变量说明：total_cost_usd 表示当前步骤使用的 total_cost_usd 值。
    total_cost_usd: float
    # 变量说明：avg_cost_usd 表示当前步骤使用的 avg_cost_usd 值。
    avg_cost_usd: float
    # 变量说明：cache_hit_rate 表示当前步骤使用的 cache_hit_rate 值。
    cache_hit_rate: float


# 类职责：定义 UsageWorkspaceRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class UsageWorkspaceRead(BaseModel):
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: str | None
    # 变量说明：name 表示当前对象名称。
    name: str | None
    # 变量说明：path 表示当前文件或目录路径。
    path: str | None
    # 变量说明：requests 表示当前流程使用的 requests 集合。
    requests: int
    # 变量说明：tokens 表示当前流程使用的 tokens 集合。
    tokens: int
    # 变量说明：total_cost_usd 表示当前步骤使用的 total_cost_usd 值。
    total_cost_usd: float
    # 变量说明：avg_cost_usd 表示当前步骤使用的 avg_cost_usd 值。
    avg_cost_usd: float
    # 变量说明：cache_hit_rate 表示当前步骤使用的 cache_hit_rate 值。
    cache_hit_rate: float


# 类职责：定义 DashboardRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class DashboardRead(BaseModel):
    # 变量说明：workspaces 表示当前流程使用的 workspaces 集合。
    workspaces: int
    # 变量说明：agents 表示当前流程使用的 agents 集合。
    agents: int
    # 变量说明：sessions 表示当前流程使用的 sessions 集合。
    sessions: int
    # 变量说明：active_runs 表示当前流程使用的 active_runs 集合。
    active_runs: int
    # 变量说明：pending_approvals 表示当前流程使用的 pending_approvals 集合。
    pending_approvals: int
    # 变量说明：model_connections 表示当前流程使用的 model_connections 集合。
    model_connections: int


# 类职责：定义 DelegatedTaskRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class DelegatedTaskRead(ORMModel):
    """Read-only child-Agent delegation state for a conversation."""

    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
    parent_run_id: str
    # 变量说明：parent_session_id 表示parent_session 对象的唯一标识。
    parent_session_id: str | None
    # 变量说明：child_run_id 表示child_run 对象的唯一标识。
    child_run_id: str | None
    # 变量说明：child_agent_id 表示child_agent 对象的唯一标识。
    child_agent_id: str | None
    # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
    plan_step_id: str | None
    # 变量说明：teammate_id 表示teammate 对象的唯一标识。
    teammate_id: str | None
    # 变量说明：title 表示当前步骤使用的 title 值。
    title: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：status 表示当前对象或运行的状态。
    status: str
    # 变量说明：result 表示本步骤产生的结果。
    result: dict[str, Any]
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: datetime


# 类职责：定义 BackgroundJobRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class BackgroundJobRead(ORMModel):
    """Durable background command state visible from a conversation."""

    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：run_id 表示当前运行标识。
    run_id: str | None
    # 变量说明：session_id 表示所属会话标识。
    session_id: str | None
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id: str | None
    # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
    plan_step_id: str | None
    # 变量说明：command 表示当前步骤使用的 command 值。
    command: str
    # 变量说明：shell 表示当前步骤使用的 shell 值。
    shell: str
    # 变量说明：status 表示当前对象或运行的状态。
    status: str
    # 变量说明：timeout_seconds 表示当前流程使用的 timeout_seconds 集合。
    timeout_seconds: int
    # 变量说明：pid 表示当前步骤使用的 pid 值。
    pid: int | None
    # 变量说明：exit_code 表示当前步骤使用的 exit_code 值。
    exit_code: int | None
    # 变量说明：log_path 表示log_path 对应的文件系统位置。
    log_path: str
    # 变量说明：output_preview 表示当前步骤使用的 output_preview 值。
    output_preview: str
    # 变量说明：error 表示当前捕获或准备上报的错误。
    error: str | None
    # 变量说明：started_at 表示started_at 对应的时间信息。
    started_at: datetime | None
    # 变量说明：finished_at 表示finished_at 对应的时间信息。
    finished_at: datetime | None
    # 变量说明：observed_at 表示observed_at 对应的时间信息。
    observed_at: datetime | None
    # 变量说明：observed_by_run_id 表示observed_by_run 对象的唯一标识。
    observed_by_run_id: str | None
    # 变量说明：waiting_run_id 表示waiting_run 对象的唯一标识。
    waiting_run_id: str | None
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: datetime


# 类职责：定义 TeammateRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class TeammateRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：team_id 表示team 对象的唯一标识。
    team_id: str
    # 变量说明：agent_id 表示智能体标识。
    agent_id: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：role 表示当前步骤使用的 role 值。
    role: str
    # 变量说明：status 表示当前对象或运行的状态。
    status: str
    # 变量说明：current_plan_step_id 表示current_plan_step 对象的唯一标识。
    current_plan_step_id: str | None
    # 变量说明：last_run_id 表示last_run 对象的唯一标识。
    last_run_id: str | None
    # 变量说明：workspace_mode 表示当前步骤使用的 workspace_mode 值。
    workspace_mode: str
    # 变量说明：worktree_path 表示worktree_path 对应的文件系统位置。
    worktree_path: str | None
    # 变量说明：branch_name 表示当前步骤使用的 branch_name 值。
    branch_name: str | None
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: datetime


# 类职责：定义 CollaborationMessageRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class CollaborationMessageRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：team_id 表示team 对象的唯一标识。
    team_id: str
    # 变量说明：sender_worker_id 表示sender_worker 对象的唯一标识。
    sender_worker_id: str | None
    # 变量说明：recipient_worker_id 表示recipient_worker 对象的唯一标识。
    recipient_worker_id: str | None
    # 变量说明：message_type 表示当前步骤使用的 message_type 值。
    message_type: str
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload: dict[str, Any]
    # 变量说明：read_at 表示read_at 对应的时间信息。
    read_at: datetime | None
    # 变量说明：created_at 表示创建时间。
    created_at: datetime


# 类职责：定义 CollaborationEventRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class CollaborationEventRead(ORMModel):
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：task_id 表示任务标识。
    task_id: str | None
    # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
    plan_step_id: str | None
    # 变量说明：run_id 表示当前运行标识。
    run_id: str | None
    # 变量说明：source_kind 表示当前步骤使用的 source_kind 值。
    source_kind: str
    # 变量说明：source_id 表示source 对象的唯一标识。
    source_id: str
    # 变量说明：event_type 表示当前步骤使用的 event_type 值。
    event_type: str
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload: dict[str, Any]
    # 变量说明：consumed_at 表示consumed_at 对应的时间信息。
    consumed_at: datetime | None
    # 变量说明：consumer_run_id 表示consumer_run 对象的唯一标识。
    consumer_run_id: str | None
    # 变量说明：created_at 表示创建时间。
    created_at: datetime
