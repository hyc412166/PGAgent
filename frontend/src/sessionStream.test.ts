// 本测试文件验证 sessionStream 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'
import { appendAssistantDelta, applyAssistantStreamEvent, assistantItemsText, composerSurface, hasPersistedRunReply, isCurrentSessionRun, isResumableWaitingRun, isTerminalRunStatus, isTerminalRunStreamEvent, nextRunStreamPhase, parseRunStreamEvent, rememberRunStreamEvent, restoreAssistantItemsFromEvents, runStatusPhase, runStreamPhase, shouldMarkApprovalResuming, shouldRefreshConversationAfterApprovalDecision, shouldShowStoppedRunNotice, shouldStartHistoryScroll, visibleSessionItems } from './sessionStream'
import { removePendingApproval } from './features/sessions/sessionState'

// 测试分组：会话 SSE 事件。
describe('会话 SSE 事件', () => {
  // 测试场景：所有没有持久化回复的终态都显示兜底提示。
  it('所有没有持久化回复的终态都显示兜底提示', () => {
    expect(shouldShowStoppedRunNotice({ id: 'r1', status: 'stopped', stop_reason: '已连续 4 步没有产生有效进展' }, new Set())).toBe(true)
    expect(shouldShowStoppedRunNotice({ id: 'r1', status: 'stopped', stop_reason: 'user_interrupted' }, new Set())).toBe(true)
    expect(shouldShowStoppedRunNotice({ id: 'r1', status: 'stopped', stop_reason: 'approval_rejected' }, new Set())).toBe(true)
    expect(shouldShowStoppedRunNotice({ id: 'r1', status: 'stopped', stop_reason: 'delegated_child_awaiting_approval' }, new Set())).toBe(false)
    expect(shouldShowStoppedRunNotice({ id: 'r1', status: 'stopped', stop_reason: 'waiting_background' }, new Set())).toBe(false)
    expect(shouldShowStoppedRunNotice({ id: 'r1', status: 'stopped', stop_reason: 'delegated_child_waiting_event' }, new Set())).toBe(false)
    expect(shouldShowStoppedRunNotice({ id: 'r1', status: 'stopped', stop_reason: 'failure' }, new Set(['r1']))).toBe(false)
    expect(shouldShowStoppedRunNotice({ id: 'r1', status: 'stopped', stop_reason: 'failure' }, new Set(), false)).toBe(false)
    expect(shouldShowStoppedRunNotice({ id: 'r2', status: 'failed', error_message: 'provider rejected context' }, new Set())).toBe(true)
    expect(shouldShowStoppedRunNotice({ id: 'r2', status: 'failed', error_message: 'provider rejected context' }, new Set(['r2']))).toBe(false)
    expect(shouldShowStoppedRunNotice({ id: 'r1', status: 'completed' }, new Set())).toBe(false)
  })

  it('从 run_state 的 code 读取可恢复等待原因', () => {
    expect(isTerminalRunStreamEvent({ type: 'run_state', status: 'stopped', code: 'waiting_background', reason: '后台作业仍在运行' })).toBe(false)
    expect(runStreamPhase({ type: 'run_state', status: 'stopped', code: 'delegated_child_waiting_event' })).toContain('等待子 Agent')
  })

  // 测试场景：按 run 或 turn 标识判断当前运行是否已有持久化终态回复。
  it('按 run 或 turn 标识判断当前运行是否已有持久化终态回复', () => {
    const messages = [
      { role: 'assistant', turn_id: 'turn-1', message_kind: 'terminal', metadata: { run_id: 'run-1' } },
      { role: 'assistant', metadata: { runtime_run_id: 'run-2' } },
    ]
    expect(hasPersistedRunReply(messages, 'run-1')).toBe(true)
    expect(hasPersistedRunReply(messages, 'missing', 'turn-1')).toBe(true)
    expect(hasPersistedRunReply(messages, 'run-2')).toBe(false)
  })
  // 测试场景：解析生命周期与文本增量。
  it('解析生命周期与文本增量', () => {
    const delta = parseRunStreamEvent('{"type":"assistant_delta","delta":"你好"}')
    expect(delta).toEqual({ type: 'assistant_delta', delta: '你好' })
    expect(appendAssistantDelta('开始：', delta!)).toBe('开始：你好')
    expect(runStreamPhase(delta!)).toBe('正在回复…')
  })

  it('把 commentary 中间回复显示为思考，并保留它直到下一条可见阶段事件', () => {
    const commentary = { type: 'assistant_message_completed', phase: 'commentary' }
    expect(runStreamPhase(commentary)).toBe('思考中…')
    expect(nextRunStreamPhase('思考中…', { type: 'model_response_completed' })).toBe('思考中…')
    expect(nextRunStreamPhase('思考中…', { type: 'model_step_finished' })).toBe('思考中…')
    expect(nextRunStreamPhase('思考中…', { type: 'tool_started', tool_name: 'update_plan' })).toBe('正在调用 update_plan…')
    expect(runStreamPhase({ type: 'assistant_message_completed', phase: 'final_answer' })).toBe('已生成一段回复')
  })

  it('明确展示上下文压缩阶段并在完成后回到等待模型', () => {
    expect(runStreamPhase({ type: 'context_compaction_started' })).toBe('正在压缩上下文…')
    expect(runStreamPhase({ type: 'context_compaction_finished' })).toBe('上下文压缩完成，继续思考…')
    expect(runStreamPhase({ type: 'context_compacted' })).toBe('上下文压缩完成，继续思考…')
    expect(runStreamPhase({ type: 'context_compaction_failed' })).toBe('上下文压缩失败，继续处理…')
    expect(runStreamPhase({ type: 'context_prepared' })).toBe('正在准备上下文…')
  })

  it('按 response/item 保留有序助手正文，工具事件不会清空此前内容', () => {
    let items = applyAssistantStreamEvent([], { type: 'assistant_message_started', response_id: 'resp-1', item_id: 'item-1', output_index: 0 })
    items = applyAssistantStreamEvent(items, { type: 'assistant_message_delta', response_id: 'resp-1', item_id: 'item-1', delta: '先检查 ' })
    items = applyAssistantStreamEvent(items, { type: 'tool_started', tool_name: 'read_file' })
    items = applyAssistantStreamEvent(items, { type: 'assistant_message_delta', response_id: 'resp-1', item_id: 'item-1', delta: '结果。' })

    expect(items).toEqual([expect.objectContaining({ id: 'resp-1:item-1', content: '先检查 结果。', status: 'streaming' })])
  })

  it('兼容旧 assistant_delta 时将其归类为最终回复', () => {
    const items = applyAssistantStreamEvent([], { type: 'assistant_delta', delta: '最终正文' })

    expect(items[0]).toEqual(expect.objectContaining({ content: '最终正文', phase: 'final_answer' }))
  })

  it('从持久化事件恢复断线期间的助手正文', () => {
    const items = restoreAssistantItemsFromEvents([
      { type: 'assistant_message_started', response_id: 'resp-reconnect', item_id: 'item-1', output_index: 0 },
      { type: 'assistant_message_delta', response_id: 'resp-reconnect', item_id: 'item-1', delta: '断线前后正文' },
      { type: 'assistant_message_completed', response_id: 'resp-reconnect', item_id: 'item-1', content: '断线前后正文', output_index: 0 },
    ])
    expect(assistantItemsText(items)).toBe('断线前后正文')
    expect(items[0].status).toBe('completed')
  })

  it('完成 item 原子替换正文，model response completed 不改变 live item', () => {
    let items = applyAssistantStreamEvent([], { type: 'assistant_message_delta', response_id: 'resp-2', item_id: 'item-2', delta: '草稿' })
    items = applyAssistantStreamEvent(items, { type: 'model_response_completed', response_id: 'resp-2' })
    expect(items[0].status).toBe('streaming')
    items = applyAssistantStreamEvent(items, { type: 'assistant_message_completed', response_id: 'resp-2', item_id: 'item-2', content: '最终正文', output_index: 0 })
    expect(items).toEqual([expect.objectContaining({ id: 'resp-2:item-2', content: '最终正文', status: 'completed' })])
  })

  it('response id 稍后到达时按 item id 合并而不重复正文', () => {
    let items = applyAssistantStreamEvent([], { type: 'assistant_message_started', item_id: 'item-late', output_index: 1 })
    items = applyAssistantStreamEvent(items, { type: 'assistant_message_delta', item_id: 'item-late', output_index: 1, delta: '流式正文' })
    items = applyAssistantStreamEvent(items, {
      type: 'assistant_message_completed',
      response_id: 'resp-late',
      item_id: 'item-late',
      output_index: 1,
      content: '流式正文',
      phase: 'commentary',
    })

    expect(items).toHaveLength(1)
    expect(items[0]).toEqual(expect.objectContaining({
      id: 'resp-late:item-late',
      responseId: 'resp-late',
      itemId: 'item-late',
      outputIndex: 1,
      content: '流式正文',
      status: 'completed',
    }))
  })

  // 测试场景：在中断终态用服务端 partial_output 补齐丢失的流片段。
  it('在中断终态用服务端 partial_output 补齐丢失的流片段', () => {
    expect(appendAssistantDelta('你好', { type: 'run_stopped', partial_output: '你好，世界' })).toBe('你好，世界')
    expect(appendAssistantDelta('你好，世界', { type: 'run_stopped', partial_output: '你好' })).toBe('你好，世界')
  })

  // 测试场景：识别工具、审批与终态。
  it('识别工具、审批与终态', () => {
    expect(runStreamPhase({ type: 'tool_started', tool_name: 'read_file' })).toBe('正在调用 read_file…')
    expect(runStreamPhase({ type: 'tool_call', tool_name: 'webfetch' })).toBe('正在调用 webfetch…')
    expect(runStreamPhase({ type: 'tool_call', tool_name: 'web_search' })).toBe('正在调用 web_search…')
    expect(runStreamPhase({ type: 'plan_updated' })).toBe('正在按计划推进…')
    expect(runStreamPhase({ type: 'approval_requested' })).toBe('等待你的审批')
    expect(runStreamPhase({ type: 'delegated_child_awaiting_approval' })).toBe('子 Agent 正等待你的审批')
    expect(isTerminalRunStreamEvent({ type: 'run_completed' })).toBe(true)
    expect(isTerminalRunStreamEvent({ type: 'model_step_started' })).toBe(false)
    expect(isTerminalRunStreamEvent({ type: 'integration_failed' })).toBe(true)
    expect(isTerminalRunStreamEvent({ type: 'run_state', status: 'completed', terminal: true })).toBe(true)
    expect(isTerminalRunStatus('failed')).toBe(true)
    expect(runStatusPhase('awaiting_approval')).toBe('等待你的审批')
  })

  // 测试场景：后台和子 Agent 等待态保持 SSE 打开而不是误判为终态。
  it('后台和子 Agent 等待态保持 SSE 打开而不是误判为终态', () => {
    expect(isResumableWaitingRun({ status: 'stopped', stop_reason: 'waiting_background' })).toBe(true)
    expect(isResumableWaitingRun({ status: 'stopped', stop_reason: 'delegated_child_waiting_event' })).toBe(true)
    expect(isTerminalRunStreamEvent({ type: 'run_state', status: 'stopped', reason: 'waiting_background', terminal: false })).toBe(false)
    expect(isTerminalRunStreamEvent({ type: 'run_stopped', reason: 'delegated_child_waiting_event', terminal: false })).toBe(false)
    expect(isTerminalRunStreamEvent({ type: 'run_stopped', reason: 'user_interrupted' })).toBe(true)
    expect(runStreamPhase({ type: 'run_state', status: 'stopped', reason: 'waiting_background' })).toBe('等待后台任务完成…')
    expect(runStreamPhase({ type: 'run_stopped', reason: 'delegated_child_waiting_event' })).toBe('等待子 Agent 返回…')
  })

  // 测试场景：忽略无法识别的损坏事件。
  it('忽略无法识别的损坏事件', () => {
    expect(parseRunStreamEvent('not-json')).toBeNull()
    expect(parseRunStreamEvent('纯文本', 'assistant_delta')).toEqual({ type: 'assistant_delta', delta: '纯文本' })
  })

  // 测试场景：按 event_id 去重重放的文本增量。
  it('按 event_id 去重重放的文本增量', () => {
    const seen = new Set<string>()
    const event = { type: 'assistant_delta', delta: '不会重复', event_id: 'run-1-event-8' }
    expect(rememberRunStreamEvent(seen, event)).toBe(true)
    expect(rememberRunStreamEvent(seen, event)).toBe(false)
    expect(rememberRunStreamEvent(seen, { type: 'assistant_delta', delta: '下一段' }, 'run-1-event-9')).toBe(true)
    expect(rememberRunStreamEvent(seen, { type: 'assistant_delta', delta: '下一段' }, 'run-1-event-9')).toBe(false)
  })

  // 测试场景：会话切换后拒绝旧消息和旧运行的异步结果。
  it('会话切换后拒绝旧消息和旧运行的异步结果', () => {
    const oldMessages = [{ id: 'old-message' }]
    expect(visibleSessionItems('session-old', 'session-new', oldMessages)).toEqual([])
    expect(visibleSessionItems('session-new', 'session-new', oldMessages)).toBe(oldMessages)
    expect(isCurrentSessionRun('session-new', 'run-new', '', 'session-old', 'run-old')).toBe(false)
    expect(isCurrentSessionRun('session-new', '', 'run-new', 'session-new', 'run-new')).toBe(true)
  })

  // 测试场景：审批响应不能覆盖已经先到达的 SSE 状态。
  it('审批响应不能覆盖已经先到达的 SSE 状态', () => {
    expect(shouldMarkApprovalResuming('awaiting_approval', 'run-1', 'run-1')).toBe(true)
    expect(shouldMarkApprovalResuming('live', 'run-1', 'run-1')).toBe(false)
    expect(shouldMarkApprovalResuming('terminal', 'run-1', 'run-1')).toBe(false)
    expect(shouldMarkApprovalResuming('idle', '', 'run-1')).toBe(false)
    expect(shouldMarkApprovalResuming('awaiting_approval', 'run-2', 'run-1')).toBe(false)
  })
  // 测试场景：待审批时底部交互区切换为审批面板，草稿页仍保持输入框。
  it('根据待审批数量选择底部交互面板', () => {
    expect(composerSurface(1, false)).toBe('approval')
    expect(composerSurface(2, false)).toBe('approval')
    expect(composerSurface(0, false)).toBe('composer')
    expect(composerSurface(1, true)).toBe('composer')
  })
  // 测试场景：refreshes the session after a rejected child approval。
  it('refreshes the session after a rejected child approval', () => {
    expect(shouldRefreshConversationAfterApprovalDecision('reject')).toBe(true)
    expect(shouldRefreshConversationAfterApprovalDecision('approve')).toBe(false)
  })

  it('removes the decided approval before background refresh completes', () => {
    const approvals = [
      { id: 'approval-old', run_id: 'run-1' },
      { id: 'approval-next', run_id: 'run-1' },
    ]
    expect(removePendingApproval(approvals, 'approval-old')).toEqual([
      { id: 'approval-next', run_id: 'run-1' },
    ])
  })

  // 测试场景：shows MCP startup before the first model thought。
  it('shows MCP startup before the first model thought', () => {
    expect(runStreamPhase({ type: 'mcp_catalog_loading' })).toBe('正在准备 MCP 工具目录…')
    expect(runStreamPhase({ type: 'mcp_connecting' })).toBe('正在按需连接 MCP 服务…')
    expect(runStreamPhase({ type: 'mcp_server_ready' })).toBe('MCP 服务已按需连接')
    expect(runStreamPhase({ type: 'mcp_ready', tool_count: 24 })).toBe('MCP 已就绪（24 个工具）')
    expect(runStreamPhase({ type: 'mcp_degraded', tool_count: 12 })).toBe('部分 MCP 服务不可用，本轮继续使用已连接工具')
    expect(runStreamPhase({ type: 'mcp_degraded' })).toBe('部分 MCP 服务不可用，本轮继续使用已连接工具')
  })

  // 测试场景：历史会话必须等消息和思考记录完成装载后才启动到底部的平滑滚动。
  it('历史会话必须等消息和思考记录完成装载后才启动到底部的平滑滚动', () => {
    expect(shouldStartHistoryScroll(true, false, true)).toBe(false)
    expect(shouldStartHistoryScroll(true, true, false)).toBe(true)
    expect(shouldStartHistoryScroll(false, false, true)).toBe(true)
    expect(shouldStartHistoryScroll(false, true, false)).toBe(false)
  })
})
