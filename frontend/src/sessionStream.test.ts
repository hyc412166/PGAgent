import { describe, expect, it } from 'vitest'
import { appendAssistantDelta, hasPersistedRunReply, isCurrentSessionRun, isResumableWaitingRun, isTerminalRunStatus, isTerminalRunStreamEvent, parseRunStreamEvent, rememberRunStreamEvent, runStatusPhase, runStreamPhase, shouldMarkApprovalResuming, shouldRefreshConversationAfterApprovalDecision, shouldShowStoppedRunNotice, shouldStartHistoryScroll, visibleSessionItems } from './sessionStream'

describe('会话 SSE 事件', () => {
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

  it('按 run 或 turn 标识判断当前运行是否已有持久化终态回复', () => {
    const messages = [
      { role: 'assistant', turn_id: 'turn-1', message_kind: 'terminal', metadata: { run_id: 'run-1' } },
      { role: 'assistant', metadata: { runtime_run_id: 'run-2' } },
    ]
    expect(hasPersistedRunReply(messages, 'run-1')).toBe(true)
    expect(hasPersistedRunReply(messages, 'missing', 'turn-1')).toBe(true)
    expect(hasPersistedRunReply(messages, 'run-2')).toBe(false)
  })
  it('解析生命周期与文本增量', () => {
    const delta = parseRunStreamEvent('{"type":"assistant_delta","delta":"你好"}')
    expect(delta).toEqual({ type: 'assistant_delta', delta: '你好' })
    expect(appendAssistantDelta('开始：', delta!)).toBe('开始：你好')
    expect(runStreamPhase(delta!)).toBe('正在回复…')
  })

  it('在中断终态用服务端 partial_output 补齐丢失的流片段', () => {
    expect(appendAssistantDelta('你好', { type: 'run_stopped', partial_output: '你好，世界' })).toBe('你好，世界')
    expect(appendAssistantDelta('你好，世界', { type: 'run_stopped', partial_output: '你好' })).toBe('你好，世界')
  })

  it('识别工具、审批与终态', () => {
    expect(runStreamPhase({ type: 'tool_started', tool_name: 'read_file' })).toBe('正在调用 read_file…')
    expect(runStreamPhase({ type: 'tool_call', tool_name: 'webfetch' })).toBe('正在调用 webfetch…')
    expect(runStreamPhase({ type: 'approval_requested' })).toBe('等待你的审批')
    expect(runStreamPhase({ type: 'delegated_child_awaiting_approval' })).toBe('子 Agent 正等待你的审批')
    expect(isTerminalRunStreamEvent({ type: 'run_completed' })).toBe(true)
    expect(isTerminalRunStreamEvent({ type: 'model_step_started' })).toBe(false)
    expect(isTerminalRunStreamEvent({ type: 'integration_failed' })).toBe(true)
    expect(isTerminalRunStreamEvent({ type: 'run_state', status: 'completed', terminal: true })).toBe(true)
    expect(isTerminalRunStatus('failed')).toBe(true)
    expect(runStatusPhase('awaiting_approval')).toBe('等待你的审批')
  })

  it('后台和子 Agent 等待态保持 SSE 打开而不是误判为终态', () => {
    expect(isResumableWaitingRun({ status: 'stopped', stop_reason: 'waiting_background' })).toBe(true)
    expect(isResumableWaitingRun({ status: 'stopped', stop_reason: 'delegated_child_waiting_event' })).toBe(true)
    expect(isTerminalRunStreamEvent({ type: 'run_state', status: 'stopped', reason: 'waiting_background', terminal: false })).toBe(false)
    expect(isTerminalRunStreamEvent({ type: 'run_stopped', reason: 'delegated_child_waiting_event', terminal: false })).toBe(false)
    expect(isTerminalRunStreamEvent({ type: 'run_stopped', reason: 'user_interrupted' })).toBe(true)
    expect(runStreamPhase({ type: 'run_state', status: 'stopped', reason: 'waiting_background' })).toBe('等待后台任务完成…')
    expect(runStreamPhase({ type: 'run_stopped', reason: 'delegated_child_waiting_event' })).toBe('等待子 Agent 返回…')
  })

  it('忽略无法识别的损坏事件', () => {
    expect(parseRunStreamEvent('not-json')).toBeNull()
    expect(parseRunStreamEvent('纯文本', 'assistant_delta')).toEqual({ type: 'assistant_delta', delta: '纯文本' })
  })

  it('按 event_id 去重重放的文本增量', () => {
    const seen = new Set<string>()
    const event = { type: 'assistant_delta', delta: '不会重复', event_id: 'run-1-event-8' }
    expect(rememberRunStreamEvent(seen, event)).toBe(true)
    expect(rememberRunStreamEvent(seen, event)).toBe(false)
    expect(rememberRunStreamEvent(seen, { type: 'assistant_delta', delta: '下一段' }, 'run-1-event-9')).toBe(true)
    expect(rememberRunStreamEvent(seen, { type: 'assistant_delta', delta: '下一段' }, 'run-1-event-9')).toBe(false)
  })

  it('会话切换后拒绝旧消息和旧运行的异步结果', () => {
    const oldMessages = [{ id: 'old-message' }]
    expect(visibleSessionItems('session-old', 'session-new', oldMessages)).toEqual([])
    expect(visibleSessionItems('session-new', 'session-new', oldMessages)).toBe(oldMessages)
    expect(isCurrentSessionRun('session-new', 'run-new', '', 'session-old', 'run-old')).toBe(false)
    expect(isCurrentSessionRun('session-new', '', 'run-new', 'session-new', 'run-new')).toBe(true)
  })

  it('审批响应不能覆盖已经先到达的 SSE 状态', () => {
    expect(shouldMarkApprovalResuming('awaiting_approval', 'run-1', 'run-1')).toBe(true)
    expect(shouldMarkApprovalResuming('live', 'run-1', 'run-1')).toBe(false)
    expect(shouldMarkApprovalResuming('terminal', 'run-1', 'run-1')).toBe(false)
    expect(shouldMarkApprovalResuming('idle', '', 'run-1')).toBe(false)
    expect(shouldMarkApprovalResuming('awaiting_approval', 'run-2', 'run-1')).toBe(false)
  })
  it('refreshes the session after a rejected child approval', () => {
    expect(shouldRefreshConversationAfterApprovalDecision('reject')).toBe(true)
    expect(shouldRefreshConversationAfterApprovalDecision('approve')).toBe(false)
  })

  it('历史会话必须等消息和思考记录完成装载后才启动到底部的平滑滚动', () => {
    expect(shouldStartHistoryScroll(true, false, true)).toBe(false)
    expect(shouldStartHistoryScroll(true, true, false)).toBe(true)
    expect(shouldStartHistoryScroll(false, false, true)).toBe(true)
    expect(shouldStartHistoryScroll(false, true, false)).toBe(false)
  })
})
