// 本文件负责 sessionStream 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { AssistantStreamItem, FileChangeSet } from './types'

// RunStreamEvent 是后端 SSE 事件的宽松前端模型；索引签名保留不同事件携带的扩展字段。
export interface RunStreamEvent {
  type: string
  delta?: string
  tool_name?: string
  error?: string
  reason?: string
  status?: string
  terminal?: boolean
  change_set?: FileChangeSet
  event_id?: string
  [key: string]: unknown
}

// 三组集合分别定义终态事件、终态状态，以及“已停止但稍后可自动续跑”的等待原因。
const terminalTypes = new Set(['turn_completed', 'turn_failed', 'turn_stopped', 'run_completed', 'run_interrupted', 'run_stopped', 'model_failed', 'integration_failed', 'failed', 'completed', 'stopped'])
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
    case 'assistant_message_started':
    case 'assistant_message_delta': return '正在回复…'
    case 'assistant_message_completed': return '已生成一段回复'
    case 'model_response_completed': return '正在整理回复…'
    case 'tool_started':
    case 'tool_call': return event.tool_name ? `正在调用 ${event.tool_name}…` : '正在调用工具…'
    case 'tool_finished':
    case 'tool_result': return '正在读取工具结果…'
    case 'plan_updated': return '正在按计划推进…'
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
    case 'turn_completed':
    case 'completed': return '已完成'
    case 'turn_stopped':
    case 'run_interrupted':
    case 'run_stopped':
    case 'stopped': {
      const reason = String(event.reason || event.stop_reason || '')
      return isResumableWaitingRun({ status: 'stopped', reason }) ? waitingRunPhase(reason) : '已停止'
    }
    case 'model_failed':
    case 'turn_failed':
    case 'integration_failed':
    case 'failed': return '运行失败'
    default: return '处理中…'
  }
}

// 综合显式 terminal、事件类型和状态识别终态，同时排除可恢复等待点。
export function isTerminalRunStreamEvent(event: RunStreamEvent): boolean {
  if (event.terminal === false) return false
  const inferredStatus = event.status || (event.type === 'run_stopped' || event.type === 'turn_stopped' || event.type === 'stopped' ? 'stopped' : '')
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

function streamString(event: RunStreamEvent, key: string): string {
  const direct = event[key]
  if (typeof direct === 'string' && direct.trim()) return direct.trim()
  const payload = event.payload
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    const nested = (payload as Record<string, unknown>)[key]
    if (typeof nested === 'string' && nested.trim()) return nested.trim()
  }
  return ''
}

function streamNumber(event: RunStreamEvent, key: string): number | undefined {
  const direct = event[key]
  if (typeof direct === 'number' && Number.isInteger(direct) && direct >= 0) return direct
  const payload = event.payload
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    const nested = (payload as Record<string, unknown>)[key]
    if (typeof nested === 'number' && Number.isInteger(nested) && nested >= 0) return nested
  }
  return undefined
}

function assistantItemKey(event: RunStreamEvent, fallbackIndex: number): { id: string; responseId?: string; itemId?: string; outputIndex?: number } {
  const responseId = streamString(event, 'response_id') || undefined
  const itemId = streamString(event, 'item_id') || undefined
  const outputIndex = streamNumber(event, 'output_index')
  const identity = itemId || (outputIndex === undefined ? `legacy-${fallbackIndex}` : `index-${outputIndex}`)
  return { id: `${responseId || 'legacy'}:${identity}`, responseId, itemId, outputIndex }
}

function itemMatches(item: AssistantStreamItem, key: ReturnType<typeof assistantItemKey>): boolean {
  if (item.id === key.id) return true
  const sameResponse = !key.responseId || !item.responseId || item.responseId === key.responseId
  if (key.itemId && item.itemId === key.itemId && sameResponse) return true
  return key.outputIndex !== undefined && item.outputIndex === key.outputIndex && sameResponse
}

// 将细粒度正文事件折叠为稳定的有序条目；工具和 response completed 事件不会触碰这些条目。
export function applyAssistantStreamEvent(items: AssistantStreamItem[], event: RunStreamEvent): AssistantStreamItem[] {
  const type = String(event.type || event.event_type || '').toLowerCase()
  if (!['assistant_message_started', 'assistant_message_delta', 'assistant_message_completed', 'assistant_delta'].includes(type)) return items
  const next = items.map((item) => ({ ...item }))
  const key = assistantItemKey(event, next.length)
  let existingIndex = next.findIndex((item) => itemMatches(item, key))
  // 旧 SSE 没有 response/item 标识；在同一运行内沿用最后一个未完成条目，
  // 避免 started、delta、completed 三种兼容事件因为输出索引差异重复成空白消息。
  if (existingIndex < 0 && !key.responseId && !key.itemId) {
    existingIndex = next.findLastIndex((item) => item.status === 'streaming' && !item.responseId)
  }
  const content = typeof event.content === 'string'
    ? event.content
    : typeof event.delta === 'string' ? event.delta : ''
  // 旧版 assistant_delta 只承载最终正文，没有显式 phase；兼容时按最终回复处理，
  // 以免它被错误塞进可折叠的 commentary/执行详情区域。
  const phase = streamString(event, 'phase') || (type === 'assistant_delta' ? 'final_answer' : undefined)
  const status = type === 'assistant_message_completed' ? 'completed' as const : 'streaming' as const
  if (existingIndex >= 0) {
    const current = next[existingIndex]
    next[existingIndex] = {
      ...current,
      id: key.responseId || key.itemId ? key.id : current.id,
      responseId: current.responseId || key.responseId,
      itemId: current.itemId || key.itemId,
      outputIndex: current.outputIndex ?? key.outputIndex,
      phase: phase || current.phase,
      content: type === 'assistant_message_delta' || type === 'assistant_delta'
        ? `${current.content}${content}`
        : content || current.content,
      status: type === 'assistant_message_completed' ? 'completed' : current.status,
    }
    return next
  }
  const item: AssistantStreamItem = {
    ...key,
    content: type === 'assistant_message_delta' || type === 'assistant_delta' ? content : content,
    phase,
    status,
  }
  // 同一个 response 内按 output_index 排序；不同 response 保留首次到达顺序。
  const insertionIndex = key.responseId && key.outputIndex !== undefined
    ? next.findIndex((candidate) => candidate.responseId === key.responseId && candidate.outputIndex !== undefined && candidate.outputIndex > key.outputIndex!)
    : -1
  if (insertionIndex >= 0) next.splice(insertionIndex, 0, item)
  else next.push(item)
  return next
}

export function assistantItemsText(items: AssistantStreamItem[]): string {
  return items.map((item) => item.content).join('')
}

// 从持久化运行事件恢复正文；用于断线重连或页面重新进入活动运行时补齐漏收的增量。
export function restoreAssistantItemsFromEvents(events: RunStreamEvent[]): AssistantStreamItem[] {
  return events.reduce<AssistantStreamItem[]>((items, event) => applyAssistantStreamEvent(items, event), [])
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
