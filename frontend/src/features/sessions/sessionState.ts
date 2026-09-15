// 本文件实现 sessionState 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
import { emptyThoughtTimeline } from '../../thoughtTimeline'
import type { ThoughtTimelineState } from '../../thoughtTimeline'
import type { AssistantStreamItem, DelegatedTask, Message, PermissionMode, Run, Session, SessionContext, Teammate, ThinkingLevel, Workspace } from '../../types'

// 以下类型明确会话页各状态块的所有权：ownerSessionId 用于隔离切换会话前后的异步结果。
export type LiveRunState = { runId: string; phase: string; draft: string; assistantItems: AssistantStreamItem[]; status: 'idle' | 'connecting' | 'live' | 'fallback' | 'awaiting_approval' | 'terminal'; error: string; thought: ThoughtTimelineState; thinkingStatus: string }
export type OwnedSessionMessages = { ownerSessionId: string; items: Message[] }
export type OwnedSessionRuns = { ownerSessionId: string; items: Run[] }
export type OwnedSessionDelegations = { ownerSessionId: string; items: DelegatedTask[] }
export type ProjectHoverCard = { id: string; name: string; path: string; conversationCount: number; left: number; top: number }
export type DraftSessionSettings = { model_connection_id: string | null; model_id: string | null; thinking_level: ThinkingLevel; skill_ids: string[]; mcp_server_names: string[]; permission_mode: PermissionMode; use_memories: boolean }
export type DraftLaunchResponse = { session: Session; run: Run; workspace?: Workspace }

// 新会话草稿、上下文和空集合的稳定初值，供 SessionsPage 重置状态时复用。
export const emptyDraftSettings: DraftSessionSettings = { model_connection_id: null, model_id: null, thinking_level: 'medium', skill_ids: [], mcp_server_names: [], permission_mode: 'smart', use_memories: true }
export const emptyDraftContext: SessionContext = { active_context_tokens: 0, auto_compact_scope_tokens: 180_000, auto_compact_scope_limit: 180_000, full_context_window_limit: 200_000, base_window_tokens_remaining: 200_000, token_limit_reached: false, used_tokens: 0, limit_tokens: 200_000, compact_threshold_tokens: 180_000, percent: 0 }
export const noDelegatedTasks: DelegatedTask[] = []
export const noTeammates: Teammate[] = []

// 创建全新的实时运行状态，避免上一轮草稿、错误或思考时间线泄漏到下一轮。
export function emptyLiveRun(): LiveRunState {
  return { runId: '', phase: '', draft: '', assistantItems: [], status: 'idle', error: '', thought: emptyThoughtTimeline, thinkingStatus: '' }
}

// 页面重新进入活动运行时，以后端持久化时间恢复计时；仅在时间缺失或无效时使用当前时间。
export function runThinkingStartedAt(startedAt: string | undefined, now = Date.now()): number {
  if (!startedAt) return now
  // SQLite 取出的 UTC datetime 可能不带时区；显式补 Z，避免浏览器按本地时区解释。
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(startedAt) ? startedAt : `${startedAt}Z`
  const parsed = Date.parse(normalized)
  return Number.isFinite(parsed) ? parsed : now
}

// EventSource 需要逐个注册的后端命名事件全集，与 useRunTransport 的事件处理入口对应。
export const runStreamEventNames = [
  'run_state', 'run_received', 'context_prepared', 'context_resumed', 'context_compacted',
  'context_compaction_started', 'context_compaction_finished', 'context_compaction_failed',
  'model_step_started', 'model_retry', 'progress', 'agent_progress', 'thought_summary',
  'mcp_connecting', 'mcp_server_ready', 'mcp_ready', 'mcp_degraded',
  'thought_delta', 'activity_update', 'assistant_delta', 'tool_started', 'tool_call',
  'assistant_message_started', 'assistant_message_delta', 'assistant_message_completed', 'model_response_completed',
  'tool_finished', 'tool_result', 'completion_verification_started',
  'completion_verification_rejected', 'completion_verification_passed', 'approval_requested',
  'approval_granted', 'delegated_child_started', 'delegated_child_continuation_started',
  'delegated_child_awaiting_approval', 'delegated_child_waiting_background',
  'delegated_child_completed', 'delegated_child_stopped', 'delegated_child_failed',
  'background_continuation_started', 'run_completed', 'run_interrupted', 'run_stopped',
  'model_failed', 'integration_failed',
] as const

// 页面据此判断运行是否仍占用输入区，以及是否允许中断。
export const activeRunStatuses = new Set(['received', 'running', 'preparing_context', 'planning', 'acting', 'observing', 'verifying', 'awaiting_approval'])
