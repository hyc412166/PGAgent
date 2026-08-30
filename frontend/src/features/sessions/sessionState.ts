import { emptyThoughtTimeline } from '../../thoughtTimeline'
import type { ThoughtTimelineState } from '../../thoughtTimeline'
import type { DelegatedTask, Message, PermissionMode, Run, Session, SessionContext, Teammate, ThinkingLevel, Workspace } from '../../types'

export type LiveRunState = { runId: string; phase: string; draft: string; status: 'idle' | 'connecting' | 'live' | 'fallback' | 'awaiting_approval' | 'terminal'; error: string; thought: ThoughtTimelineState; thinkingStatus: string }
export type OwnedSessionMessages = { ownerSessionId: string; items: Message[] }
export type OwnedSessionRuns = { ownerSessionId: string; items: Run[] }
export type OwnedSessionDelegations = { ownerSessionId: string; items: DelegatedTask[] }
export type ProjectHoverCard = { id: string; name: string; path: string; conversationCount: number; left: number; top: number }
export type DraftSessionSettings = { model_connection_id: string | null; model_id: string | null; thinking_level: ThinkingLevel; skill_ids: string[]; mcp_server_names: string[]; permission_mode: PermissionMode; use_memories: boolean }
export type DraftLaunchResponse = { session: Session; run: Run; workspace?: Workspace }

export const emptyDraftSettings: DraftSessionSettings = { model_connection_id: null, model_id: null, thinking_level: 'medium', skill_ids: [], mcp_server_names: [], permission_mode: 'smart', use_memories: true }
export const emptyDraftContext: SessionContext = { used_tokens: 0, limit_tokens: 200_000, compact_threshold_tokens: 180_000, percent: 0 }
export const noDelegatedTasks: DelegatedTask[] = []
export const noTeammates: Teammate[] = []

export function emptyLiveRun(): LiveRunState {
  return { runId: '', phase: '', draft: '', status: 'idle', error: '', thought: emptyThoughtTimeline, thinkingStatus: '' }
}

export const runStreamEventNames = [
  'run_state', 'run_received', 'context_prepared', 'context_resumed', 'context_compacted',
  'context_compaction_started', 'context_compaction_finished', 'context_compaction_failed',
  'model_step_started', 'model_retry', 'progress', 'agent_progress', 'thought_summary',
  'mcp_connecting', 'mcp_ready', 'mcp_degraded',
  'thought_delta', 'activity_update', 'assistant_delta', 'tool_started', 'tool_call',
  'tool_finished', 'tool_result', 'completion_verification_started',
  'completion_verification_rejected', 'completion_verification_passed', 'approval_requested',
  'approval_granted', 'delegated_child_started', 'delegated_child_continuation_started',
  'delegated_child_awaiting_approval', 'delegated_child_waiting_background',
  'delegated_child_completed', 'delegated_child_stopped', 'delegated_child_failed',
  'background_continuation_started', 'run_completed', 'run_interrupted', 'run_stopped',
  'model_failed', 'integration_failed',
] as const

export const activeRunStatuses = new Set(['received', 'running', 'preparing_context', 'planning', 'acting', 'observing', 'verifying', 'awaiting_approval'])
