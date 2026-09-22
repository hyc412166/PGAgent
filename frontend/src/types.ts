// 本文件集中定义前端与后端接口共享的数据结构；页面、状态管理和请求层均以这些类型约束数据流。
// ApiRecord 表示尚未收窄的后端对象，转换函数会从中安全提取字段。
export type ApiRecord = Record<string, unknown>

// Health 是健康检查返回值，用于显示服务版本和依赖状态。
export interface Health {
  status?: string
  name?: string
  version?: string
}

// McpServer 描述可由会话选择的 MCP 进程配置及其连接健康状态。
export interface McpServer {
  name: string
  command: string
  args: string[]
  enabled: boolean
  transport: 'stdio' | 'streamable_http'
  runtime_status: 'inactive' | 'dormant' | 'ready' | 'degraded' | 'failed'
  tool_count: number
}

// Workspace 表示项目根目录；Session 通过 workspace_id 归属到项目树。
export interface Workspace {
  id: string
  name: string
  path?: string
  root_path?: string
  description?: string
  validation_runtime?: {
    kind: 'local' | 'docker'
    image?: string | null
  }
  created_at?: string
  updated_at?: string
}

// MemoryRecord 是持久记忆条目，包含来源、类型、状态和检索元数据。
export interface MemoryRecord {
  id: string
  scope: 'global' | 'workspace' | 'session'
  scope_id: string | null
  name: string
  title: string
  memory_type: 'user' | 'feedback' | 'project' | 'reference'
  description: string
  content: string
  tags: string[]
  pinned: boolean
  status: 'active' | 'superseded' | 'archived'
  source_session_id?: string | null
  source_turn_id?: string | null
  superseded_by?: string | null
  created_at?: string
  updated_at?: string
}

// PersonalizationSettings 保存会注入 Agent 上下文的用户级自定义指令。
export interface PersonalizationSettings {
  custom_instructions: string
  effective_instructions: string
  agents_path: string
  effective_path: string
  override_active: boolean
  max_characters: number
}

// MemorySettings 控制全局记忆能力是否启用。
export interface MemorySettings {
  enabled: boolean
}

// PermissionSettings 保存后续新会话继承的最近一次权限选择。
export interface PermissionSettings {
  permission_mode: PermissionMode
}

// AgentProfile 聚合角色提示、默认模型/思考等级以及允许使用的工具和技能。
export interface AgentProfile {
  id: string
  name: string
  role?: string
  description?: string
  system_prompt?: string
  model?: string
  model_id?: string
  connection_id?: string
  model_connection_id?: string
  thinking_level?: string
  mode?: string
  workflow_profile_id?: 'auto' | 'general' | 'coding' | 'review' | 'debug'
  enabled?: boolean
  is_default?: boolean
  status?: string
  tools?: string[]
  tool_ids?: string[]
  skill_ids?: string[]
  created_at?: string
}

// Session 是一次持续对话的配置与元数据，后续 Message、Run 均通过 session_id 关联。
export interface Session {
  id: string
  title?: string
  workspace_id?: string
  agent_id?: string
  model_connection_id?: string
  model_id?: string
  thinking_level?: ThinkingLevel
  use_memories?: boolean
  skill_ids?: string[]
  mcp_server_names?: string[]
  permission_mode?: PermissionMode
  status?: string
  created_at?: string
  updated_at?: string
}

// ThinkingLevel 和 PermissionMode 是编辑器可提交的两组枚举配置。
export type ThinkingLevel = 'low' | 'medium' | 'high' | 'xhigh'
export type PermissionMode = 'ask' | 'smart' | 'full'

// ToolCatalogItem 描述 Agent 能力选择器中的内置工具。
export interface ToolCatalogItem {
  id: string
  name: string
  label?: string
  description?: string
  category?: string
  risk_level?: string
  enabled?: boolean
  is_builtin?: boolean
  availability?: string
  requires_approval?: boolean
}

// SkillCatalogItem 表示已安装技能及其本地来源信息。
export interface SkillCatalogItem {
  id: string
  slug?: string
  name: string
  description?: string
  source?: string
  source_url?: string
  root_path?: string
  version?: string
  enabled?: boolean
  installed_at?: string
}

// SkillMarketplaceItem 是远程市场卡片数据，可进一步获取安装预览。
export interface SkillMarketplaceItem {
  id: string
  slug?: string
  name: string
  source?: string
  source_url?: string
  market_url?: string
  installs?: number
  change?: number
  installs_yesterday?: number
  is_official?: boolean
  official_owner?: string
  is_duplicate?: boolean
}

// SkillMarketplaceSearch 描述市场状态或搜索响应中的结果与提示信息。
export interface SkillMarketplaceSearch {
  provider?: string
  available?: boolean
  message?: string
  items?: SkillMarketplaceItem[]
}

// SkillMarketplaceView 是市场浏览榜单的固定视图集合。
export type SkillMarketplaceView = 'all-time' | 'trending' | 'hot' | 'curated'

// SkillMarketplaceCategory 把市场条目按稳定分类 ID 组织为榜单。
export interface SkillMarketplaceCategory {
  id: string
  label?: string
  name?: string
  title?: string
  description?: string
  query?: string
  items?: SkillMarketplaceItem[]
  updated_at?: string
  refreshed_at?: string
}

// SkillMarketplaceLeaderboards 在基础市场响应上增加分类榜与建议刷新间隔。
export interface SkillMarketplaceLeaderboards extends SkillMarketplaceSearch {
  categories?: SkillMarketplaceCategory[]
  updated_at?: string
  refreshed_at?: string
  expires_at?: string
  refresh_after_seconds?: number
  refresh_interval_seconds?: number
  ttl_seconds?: number
  cached?: boolean
}

// SkillMarketplaceBrowse 表示某一榜单视图的浏览结果。
export interface SkillMarketplaceBrowse extends SkillMarketplaceSearch {
  view?: SkillMarketplaceView
  page?: number
  has_more?: boolean
  total?: number | null
}

// SkillInstallPreview 列出安装前将写入的文件和安全提示。
export interface SkillInstallPreview {
  installed: boolean
  source_url: string
  candidates?: string[]
  files?: Array<{ path: string; size: number }>
  skill?: SkillCatalogItem
  message?: string
}

// SessionContext 描述当前上下文窗口用量、压缩阈值及压缩状态。
export interface SessionContext {
  active_context_tokens?: number
  auto_compact_scope_tokens?: number
  auto_compact_scope_limit?: number
  full_context_window_limit?: number
  base_window_tokens_remaining?: number
  token_limit_reached?: boolean
  used_tokens: number
  limit_tokens: number
  compact_threshold_tokens: number
  percent: number
  last_compaction_at?: string
}

// FolderSelection 是桌面文件夹选择接口的结果。
export interface FolderSelection {
  path?: string
  cancelled?: boolean
}

// UsageSummary 汇总给定日期范围内的 token、费用、会话和运行总量。
export interface UsageSummary {
  total_requests: number
  input_tokens: number
  output_tokens: number
  cache_creation_tokens: number
  cache_read_tokens: number
  total_tokens: number
  total_cost_usd: number
  cache_hit_rate: number
}

// ModelUsage 按模型连接聚合 token、费用和调用次数。
export interface ModelUsage {
  provider?: string
  model_id: string
  requests: number
  tokens: number
  total_cost_usd: number
  avg_cost_usd: number
  cache_hit_rate: number
}

// UsageBreakdownItem 是用量排行组件消费的统一行结构。
export interface UsageBreakdownItem {
  id: string
  title: string
  path?: string
  requests: number
  tokens: number
  total_cost_usd: number
  avg_cost_usd: number
  cache_hit_rate: number
}

// UsageSession 表示按会话聚合的用量明细。
export interface UsageSession {
  session_id: string | null
  title: string | null
  requests: number
  tokens: number
  total_cost_usd: number
  avg_cost_usd: number
  cache_hit_rate: number
}

// UsageWorkspace 表示按项目工作区聚合的用量明细。
export interface UsageWorkspace {
  workspace_id: string | null
  name: string | null
  path: string | null
  requests: number
  tokens: number
  total_cost_usd: number
  avg_cost_usd: number
  cache_hit_rate: number
}

// Message 是持久化对话消息；metadata 用于关联 run_id、附件和终态来源。
export interface Message {
  id: string
  session_id?: string
  role: 'user' | 'assistant' | 'system' | 'tool' | string
  content: string
  created_at?: string
  tool_name?: string
  tool_call_id?: string
  turn_id?: string
  message_kind?: string
  status?: string
  metadata?: ApiRecord
  citations?: Array<{ url: string; title: string }>
}

// AssistantStreamItem 是单次运行中按 response/item 顺序累计的可见正文；它与工具、终态事件分离，避免正文被工具阶段重置。
export interface AssistantStreamItem {
  id: string
  responseId?: string
  itemId?: string
  outputIndex?: number
  content: string
  phase?: 'commentary' | 'final_answer' | 'unknown' | string
  status: 'streaming' | 'completed'
}

// RunEvent 是已落盘运行事件，结构与实时 SSE 相近但包含服务端时间戳和 payload。
export interface RunEvent {
  id?: string
  run_id?: string
  sequence?: number
  trace_id?: string | null
  step?: number
  type?: string
  event_type?: string
  phase?: string
  message?: string
  created_at?: string
  payload?: ApiRecord
}

// FileChangeRecord 是运行时间线和最终回复共用的单文件变更契约。
export interface FileChangeRecord {
  path: string
  operation: 'add' | 'update' | 'delete' | string
  added_lines?: number
  deleted_lines?: number
  line_count?: number | null
  first_changed_line?: number | null
  diff?: string
  diff_truncated?: boolean
  binary?: boolean
}

export interface FileChangeSelection {
  runId: string
  change: FileChangeRecord
}

// FileChangeSet 聚合一次工具调用或一轮运行产生的文件变化。
export interface FileChangeSet {
  status?: string
  source?: string
  file_count: number
  added_lines?: number
  deleted_lines?: number
  files: FileChangeRecord[]
}

// FileContentRead 是右侧文件阅读器读取当前工作区文本的返回值。
export interface FileContentRead {
  run_id: string
  path: string
  content: string
  line_count: number
  truncated?: boolean
  binary?: boolean
}

// RunEventFilters 与 RunEventPage 对应运行诊断接口的筛选条件和游标分页结果。
export interface RunEventFilters {
  event_type?: string
  step?: number
  errors_only?: boolean
  before?: number
  limit?: number
}

export interface RunEventPage {
  items: RunEvent[]
  next_before: number | null
}

// ToolResultPage 是历史运行中单次工具结果的字符分页。
export interface ToolResultPage {
  tool_call_id: string
  tool_name: string
  ok: boolean
  content: string
  offset: number
  next_offset: number
  total_chars: number
  eof: boolean
}

// Run 描述一轮 Agent 执行的生命周期、结果、错误、计划和关联事件。
export interface Run {
  id: string
  session_id?: string
  agent_id?: string
  turn_id?: string
  task_id?: string
  plan_step_id?: string
  run_kind?: 'initial' | 'continuation' | 'recovery'
  resumed_from_run_id?: string
  agent_name?: string
  session_title?: string
  title?: string
  status?: string
  phase?: string
  step_count?: number
  current_step?: number
  tool_call_count?: number
  tool_calls?: number
  input_tokens?: number
  output_tokens?: number
  stop_reason?: string
  error_code?: string
  error_message?: string
  created_at?: string
  started_at?: string
  finished_at?: string
  updated_at?: string
  events?: RunEvent[]
}

// PlanStep 表示运行计划中的单个可跟踪步骤及其完成状态。
export interface PlanStep {
  id: string
  external_id: string
  position: number
  title: string
  description?: string
  status: 'pending' | 'in_progress' | 'needs_recovery' | 'blocked' | 'completed' | 'failed' | 'cancelled'
  completed_work?: unknown[]
  remaining_work?: unknown[]
  next_action?: string
  result?: string
  evidence?: unknown[]
  depends_on?: string[]
  executor_kind?: 'main' | 'subagent' | 'background' | string
  assigned_agent_id?: string
  assigned_run_id?: string
  claim_owner?: string
  workspace_mode?: 'shared' | 'worktree' | string
  worktree_path?: string
  attempt?: number
  error?: string
  last_run_id?: string
}

// DurableTask 表示可跨页面持续执行的后台任务及其最近进度。
export interface DurableTask {
  id: string
  session_id: string
  origin_turn_id?: string
  goal: string
  constraints?: unknown[]
  status: 'planning' | 'running' | 'waiting' | 'paused' | 'needs_recovery' | 'blocked' | 'completed' | 'failed' | 'cancelled'
  active_step_id?: string
  resume_summary?: string
  completed_at?: string
  created_at?: string
  updated_at?: string
  steps: PlanStep[]
}

// RunUsage 是单次运行的精确 token 与费用记录。
export interface RunUsage {
  run_id: string
  provider?: string | null
  model_id?: string | null
  requests: number
  input_tokens: number
  output_tokens: number
  cache_creation_tokens: number
  cache_read_tokens: number
  total_tokens: number
  total_cost_usd: number
  cache_hit_rate: number
}

// Approval 描述不可直接执行的工具动作及用户审批状态。
export interface Approval {
  id: string
  run_id?: string
  tool_name?: string
  arguments?: ApiRecord
  status?: string
  reason?: string
  created_at?: string
}

// Connection 保存模型供应商连接配置、发现到的模型及健康检查结果。
export interface Connection {
  id: string
  name: string
  base_url?: string
  provider?: string
  api_protocol?: 'responses' | 'chat_completions'
  api_key_hint?: string
  api_key?: string
  default_model?: string
  models?: string[]
  discovered_models?: string[]
  manual_models?: string[]
  disabled_models?: string[]
  thinking_level?: string
  status?: string
  enabled?: boolean
  last_checked?: string
  last_checked_at?: string
  last_error?: string
  created_at?: string
}

/**
 * A child-agent delegation belonging to one conversation.
 *
 * Delegations are runtime records owned by a parent run/session.
 */
// DelegatedTask 表示父运行委派给子 Agent 的任务及其关联运行结果。
export interface DelegatedTask {
  id: string
  parent_run_id?: string
  parent_session_id?: string
  child_run_id?: string
  child_agent_id?: string
  plan_step_id?: string
  teammate_id?: string
  child_agent_name?: string
  title: string
  description?: string
  status?: string
  result?: ApiRecord
  created_at?: string
  updated_at?: string
}

// Teammate 描述当前会话协作树中另一个 Agent 的运行状态。
export interface Teammate {
  id: string
  team_id: string
  agent_id: string
  name: string
  role: string
  status: string
  current_plan_step_id?: string
  last_run_id?: string
  workspace_mode: 'shared' | 'worktree' | string
  worktree_path?: string
  branch_name?: string
  created_at?: string
  updated_at?: string
}

// DashboardData 是总览页消费的服务端聚合指标集合。
export interface DashboardData {
  workspace_count?: number
  workspaces?: number
  agent_count?: number
  agents?: number
  session_count?: number
  sessions?: number
  active_runs?: number
  pending_approvals?: number
  total_runs?: number
  recent_runs?: Run[]
}
