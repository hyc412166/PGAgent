export type ApiRecord = Record<string, unknown>

export interface Health {
  status?: string
  name?: string
  version?: string
}

export interface Workspace {
  id: string
  name: string
  path?: string
  root_path?: string
  description?: string
  created_at?: string
  updated_at?: string
}

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
  enabled?: boolean
  is_default?: boolean
  status?: string
  tools?: string[]
  created_at?: string
}

export interface Session {
  id: string
  title?: string
  workspace_id?: string
  agent_id?: string
  model_connection_id?: string
  model_id?: string
  thinking_level?: ThinkingLevel
  status?: string
  created_at?: string
  updated_at?: string
}

export type ThinkingLevel = 'off' | 'auto' | 'low' | 'medium' | 'high' | 'xhigh'

export interface SessionContext {
  used_tokens: number
  limit_tokens: number
  compact_threshold_tokens: number
  percent: number
  last_compacted_at?: string
}

export interface FolderSelection {
  path?: string
  cancelled?: boolean
}

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

export interface ModelUsage {
  provider?: string
  model_id: string
  requests: number
  tokens: number
  total_cost_usd: number
  avg_cost_usd: number
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool' | string
  content: string
  created_at?: string
  tool_name?: string
  tool_call_id?: string
  status?: string
}

export interface RunEvent {
  id?: string
  type?: string
  event_type?: string
  phase?: string
  message?: string
  created_at?: string
  payload?: ApiRecord
}

export interface Run {
  id: string
  session_id?: string
  agent_id?: string
  agent_name?: string
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
  created_at?: string
  started_at?: string
  finished_at?: string
  updated_at?: string
  events?: RunEvent[]
}

export interface Approval {
  id: string
  run_id?: string
  tool_name?: string
  arguments?: ApiRecord
  status?: string
  reason?: string
  created_at?: string
}

export interface Connection {
  id: string
  name: string
  base_url?: string
  provider?: string
  api_key_hint?: string
  api_key?: string
  default_model?: string
  models?: string[]
  discovered_models?: string[]
  manual_models?: string[]
  thinking_level?: string
  status?: string
  enabled?: boolean
  last_checked?: string
  last_checked_at?: string
  last_error?: string
  created_at?: string
}

export interface TeamTask {
  id: string
  title: string
  description?: string
  status?: string
  assignee_agent_id?: string
  assignee_name?: string
  priority?: string
  version?: number
  lease_expires_at?: string
  created_at?: string
  updated_at?: string
}

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
