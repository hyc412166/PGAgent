export interface RunStreamEvent {
  type: string
  delta?: string
  tool_name?: string
  error?: string
  reason?: string
  status?: string
  terminal?: boolean
  event_id?: string
  [key: string]: unknown
}

const terminalTypes = new Set(['run_completed', 'run_stopped', 'model_failed', 'integration_failed', 'failed', 'completed', 'stopped'])
const terminalStatuses = new Set(['completed', 'stopped', 'failed', 'cancelled'])

export function parseRunStreamEvent(raw: string, fallbackType = ''): RunStreamEvent | null {
  try {
    const value = JSON.parse(raw) as Record<string, unknown>
    const type = String(value.type || value.event_type || fallbackType || '')
    if (!type) return null
    return { ...value, type } as RunStreamEvent
  } catch {
    return fallbackType === 'assistant_delta' ? { type: fallbackType, delta: raw } : null
  }
}

export function runStreamPhase(event: RunStreamEvent): string {
  switch (event.type) {
    case 'run_state': return runStatusPhase(event.status)
    case 'context_prepared':
    case 'context_resumed':
    case 'context_compacted': return '正在准备上下文…'
    case 'model_step_started':
    case 'model_retry': return '思考中…'
    case 'assistant_delta': return '正在回复…'
    case 'tool_started':
    case 'tool_call': return event.tool_name ? `正在调用 ${event.tool_name}…` : '正在调用工具…'
    case 'tool_finished':
    case 'tool_result': return '正在读取工具结果…'
    case 'completion_verification_started': return '正在验收任务结果…'
    case 'completion_verification_rejected': return '验收未通过，正在继续改进…'
    case 'completion_verification_passed': return '验收通过，正在完成…'
    case 'approval_requested': return '等待你的审批'
    case 'approval_granted': return '审批已通过，继续处理…'
    case 'delegated_child_started': return '子 Agent 正在处理任务…'
    case 'delegated_child_awaiting_approval': return '子 Agent 正等待你的审批'
    case 'delegated_child_completed': return '子 Agent 已返回结果'
    case 'delegated_child_continuation_started': return '主 Agent 正在汇总子 Agent 结果…'
    case 'delegated_child_stopped':
    case 'delegated_child_failed': return '子 Agent 未完成'
    case 'run_completed':
    case 'completed': return '已完成'
    case 'run_stopped':
    case 'stopped': return '已停止'
    case 'model_failed':
    case 'integration_failed':
    case 'failed': return '运行失败'
    default: return '处理中…'
  }
}

export function isTerminalRunStreamEvent(event: RunStreamEvent): boolean {
  return event.terminal === true || terminalTypes.has(event.type) || (event.type === 'run_state' && isTerminalRunStatus(event.status))
}

export function isTerminalRunStatus(status?: string): boolean {
  return terminalStatuses.has(status || '')
}

export function runStatusPhase(status?: string): string {
  switch (status) {
    case 'received':
    case 'preparing_context': return '正在准备上下文…'
    case 'planning':
    case 'running':
    case 'acting': return '思考中…'
    case 'observing': return '正在读取工具结果…'
    case 'verifying': return '正在验收任务结果…'
    case 'awaiting_approval': return '等待你的审批'
    case 'completed': return '已完成'
    case 'stopped':
    case 'cancelled': return '已停止'
    case 'failed': return '运行失败'
    default: return '处理中…'
  }
}

export function appendAssistantDelta(current: string, event: RunStreamEvent): string {
  return event.type === 'assistant_delta' && typeof event.delta === 'string' ? current + event.delta : current
}

export function rememberRunStreamEvent(seenEventIds: Set<string>, event: RunStreamEvent, lastEventId = ''): boolean {
  const eventId = String(event.event_id || lastEventId || '')
  if (!eventId) return true
  if (seenEventIds.has(eventId)) return false
  seenEventIds.add(eventId)
  return true
}

export function visibleSessionItems<T>(ownerSessionId: string, activeSessionId: string, items: T[]): T[] {
  return ownerSessionId === activeSessionId ? items : []
}

export function isCurrentSessionRun(
  activeSessionId: string,
  activeRunId: string,
  streamRunId: string,
  expectedSessionId: string,
  expectedRunId: string,
): boolean {
  return Boolean(expectedSessionId && expectedRunId)
    && activeSessionId === expectedSessionId
    && (activeRunId === expectedRunId || streamRunId === expectedRunId)
}

export function shouldMarkApprovalResuming(status: string, liveRunId: string, approvalRunId: string): boolean {
  return status === 'awaiting_approval' && Boolean(approvalRunId) && liveRunId === approvalRunId
}

export function shouldRefreshConversationAfterApprovalDecision(decision: 'approve' | 'reject'): boolean {
  // A rejected delegated child writes its terminal result directly into the
  // current session.  Approval/runs alone are insufficient to render it.
  return decision === 'reject'
}
