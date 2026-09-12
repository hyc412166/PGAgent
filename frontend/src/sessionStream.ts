// 本文件负责 sessionStream 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
// RunStreamEvent 是后端 SSE 事件的宽松前端模型；索引签名保留不同事件携带的扩展字段。
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

// 三组集合分别定义终态事件、终态状态，以及“已停止但稍后可自动续跑”的等待原因。
const terminalTypes = new Set(['run_completed', 'run_interrupted', 'run_stopped', 'model_failed', 'integration_failed', 'failed', 'completed', 'stopped'])
const terminalStatuses = new Set(['completed', 'stopped', 'failed', 'cancelled'])
const resumableWaitingReasons = new Set([
  'waiting_background',
  'delegated_child_waiting_event',
  'delegated_child_awaiting_approval',
])

// WaitingRunCandidate 只抽取判断后台等待所需字段，使该判断可复用于 Run 和 SSE 事件。
type WaitingRunCandidate = { status?: string; stop_reason?: string; reason?: string }

// 判断 stopped 是否只是持久化等待点；此类运行不能在 UI 中当作真正终止。
export function isResumableWaitingRun(run: WaitingRunCandidate | undefined): boolean {
  if (!run || run.status !== 'stopped') return false
  return resumableWaitingReasons.has(String(run.stop_reason || run.reason || ''))
}

// 将等待原因映射为状态栏阶段文案。
function waitingRunPhase(reason: string): string {
  return reason === 'waiting_background' ? '等待后台任务完成…' : '等待子 Agent 返回…'
}

// 解析 SSE data；assistant_delta 允许纯文本，以兼容无法包装成 JSON 的增量片段。
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

// 把细粒度运行事件折叠成用户可理解的当前阶段。
export function runStreamPhase(event: RunStreamEvent): string {
  switch (event.type) {
    case 'run_state': return isResumableWaitingRun({
      status: event.status,
      reason: String(event.reason || event.stop_reason || ''),
    }) ? waitingRunPhase(String(event.reason || event.stop_reason || '')) : runStatusPhase(event.status)
    case 'context_prepared':
    case 'context_resumed':
    case 'context_compaction_started':
    case 'context_compaction_finished':
    case 'context_compaction_failed':
    case 'context_compacted': return '正在准备上下文…'
    case 'model_step_started':
    case 'model_retry': return '思考中…'
    case 'mcp_catalog_loading': return '正在准备 MCP 工具目录…'
    case 'mcp_connecting': return '正在按需连接 MCP 服务…'
    case 'mcp_server_ready': return 'MCP 服务已按需连接'
    case 'mcp_ready': return Number(event.tool_count || 0) > 0
      ? `MCP 已就绪（${Number(event.tool_count)} 个工具）`
      : 'MCP 已连接'
    case 'mcp_degraded': return '部分 MCP 服务不可用，本轮继续使用已连接工具'
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
    case 'delegated_child_waiting_background': return '子 Agent 正等待后台任务完成…'
    case 'background_continuation_started': return '后台任务已完成，正在继续处理…'
    case 'delegated_child_completed': return '子 Agent 已返回结果'
    case 'delegated_child_continuation_started': return '主 Agent 正在汇总子 Agent 结果…'
    case 'delegated_child_stopped':
    case 'delegated_child_failed': return '子 Agent 未完成'
    case 'run_completed':
    case 'completed': return '已完成'
    case 'run_interrupted':
    case 'run_stopped':
    case 'stopped': {
      const reason = String(event.reason || event.stop_reason || '')
      return isResumableWaitingRun({ status: 'stopped', reason }) ? waitingRunPhase(reason) : '已停止'
    }
    case 'model_failed':
    case 'integration_failed':
    case 'failed': return '运行失败'
    default: return '处理中…'
  }
}

// 综合显式 terminal、事件类型和状态识别终态，同时排除可恢复等待点。
export function isTerminalRunStreamEvent(event: RunStreamEvent): boolean {
  if (event.terminal === false) return false
  const inferredStatus = event.status || (event.type === 'run_stopped' || event.type === 'stopped' ? 'stopped' : '')
  if (isResumableWaitingRun({
    status: inferredStatus,
    reason: String(event.reason || event.stop_reason || ''),
  })) return false
  return event.terminal === true || terminalTypes.has(event.type) || (event.type === 'run_state' && isTerminalRunStatus(event.status))
}

// 判断持久化 Run 状态是否已终止。
export function isTerminalRunStatus(status?: string): boolean {
  return terminalStatuses.has(status || '')
}

// 在没有实时事件时，将轮询到的 Run 状态映射为阶段文案。
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

// 合并回复增量；中断事件携带的 partial_output 是权威快照，可补回浏览器漏收的尾部片段。
export function appendAssistantDelta(current: string, event: RunStreamEvent): string {
  if (event.type === 'assistant_delta' && typeof event.delta === 'string') return current + event.delta
  // 停止响应可能晚于最后几段 SSE；后端在终态事件中附带权威草稿，确保中断内容仍可编辑。
  const partial = event.partial_output
  if (typeof partial === 'string' && partial.length > current.length) return partial
  return current
}

// 用事件 ID 去重重连后重复送达的 SSE；无 ID 的兼容事件默认接受。
export function rememberRunStreamEvent(seenEventIds: Set<string>, event: RunStreamEvent, lastEventId = ''): boolean {
  const eventId = String(event.event_id || lastEventId || '')
  if (!eventId) return true
  if (seenEventIds.has(eventId)) return false
  seenEventIds.add(eventId)
  return true
}

// 仅暴露属于当前会话的异步结果，防止切换会话时旧请求短暂串屏。
export function visibleSessionItems<T>(ownerSessionId: string, activeSessionId: string, items: T[]): T[] {
  return ownerSessionId === activeSessionId ? items : []
}

// 根据历史锚定是否完成及用户是否贴底，决定能否自动滚动到最新消息。
export function shouldStartHistoryScroll(
  anchoringHistory: boolean,
  historyReady: boolean,
  stickToBottom: boolean,
): boolean {
  return anchoringHistory ? historyReady : stickToBottom
}

// 底部交互区只在真实会话存在待审批项时切换；草稿始终保留输入能力。
export function composerSurface(approvalCount: number, draftActive: boolean): 'composer' | 'approval' {
  return !draftActive && approvalCount > 0 ? 'approval' : 'composer'
}

// 校验异步回调仍属于当前会话和运行，避免过期流修改新会话状态。
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

// 只有当前实时运行与审批运行一致时才展示“审批后恢复中”。
export function shouldMarkApprovalResuming(status: string, liveRunId: string, approvalRunId: string): boolean {
  return status === 'awaiting_approval' && Boolean(approvalRunId) && liveRunId === approvalRunId
}

// 拒绝子任务会直接写入终态消息，因此拒绝后需要额外刷新会话消息。
export function shouldRefreshConversationAfterApprovalDecision(decision: 'approve' | 'reject'): boolean {
  return decision === 'reject'
}

// 终止提示判断所需的最小 Run 字段。
type StoppedRunNoticeCandidate = { id: string; status?: string; stop_reason?: string; error_message?: string }

// 当终止运行尚无持久化助手回复时显示兜底提示；可恢复等待不属于失败提示。
export function shouldShowStoppedRunNotice(run: StoppedRunNoticeCandidate | undefined, repliedRunIds: Set<string>, historyReady = true): boolean {
  if (!historyReady || !run || !['stopped', 'failed'].includes(String(run.status || '')) || repliedRunIds.has(run.id)) return false
  if (run.status === 'failed') return true
  return !isResumableWaitingRun(run)
}

// PersistedRunReplyCandidate 描述识别“运行已有落盘回复”所需的消息字段。
type PersistedRunReplyCandidate = {
  role?: string
  turn_id?: string
  message_kind?: string
  metadata?: Record<string, unknown>
}

// 通过 run_id 或终态 turn_id 关联消息，避免终态同步时重复插入回复。
export function hasPersistedRunReply(
  messages: PersistedRunReplyCandidate[],
  runId: string,
  turnId = '',
): boolean {
  if (!runId && !turnId) return false
  return messages.some((message) => {
    if (message.role !== 'assistant') return false
    const metadataRunId = String(message.metadata?.run_id || '')
    const messageTurnId = String(message.turn_id || message.metadata?.turn_id || '')
    return metadataRunId === runId || (
      Boolean(turnId)
      && messageTurnId === turnId
      && (message.message_kind === 'terminal' || message.metadata?.source === 'deterministic_fallback')
    )
  })
}
