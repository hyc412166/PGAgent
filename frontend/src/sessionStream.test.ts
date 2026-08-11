import { describe, expect, it } from 'vitest'
import { appendAssistantDelta, isCurrentSessionRun, isTerminalRunStatus, isTerminalRunStreamEvent, parseRunStreamEvent, rememberRunStreamEvent, runStatusPhase, runStreamPhase, shouldMarkApprovalResuming, visibleSessionItems } from './sessionStream'

describe('会话 SSE 事件', () => {
  it('解析生命周期与文本增量', () => {
    const delta = parseRunStreamEvent('{"type":"assistant_delta","delta":"你好"}')
    expect(delta).toEqual({ type: 'assistant_delta', delta: '你好' })
    expect(appendAssistantDelta('开始：', delta!)).toBe('开始：你好')
    expect(runStreamPhase(delta!)).toBe('正在回复…')
  })

  it('识别工具、审批与终态', () => {
    expect(runStreamPhase({ type: 'tool_started', tool_name: 'read_file' })).toBe('正在调用 read_file…')
    expect(runStreamPhase({ type: 'approval_requested' })).toBe('等待你的审批')
    expect(isTerminalRunStreamEvent({ type: 'run_completed' })).toBe(true)
    expect(isTerminalRunStreamEvent({ type: 'model_step_started' })).toBe(false)
    expect(isTerminalRunStreamEvent({ type: 'integration_failed' })).toBe(true)
    expect(isTerminalRunStreamEvent({ type: 'run_state', status: 'completed', terminal: true })).toBe(true)
    expect(isTerminalRunStatus('failed')).toBe(true)
    expect(runStatusPhase('awaiting_approval')).toBe('等待你的审批')
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
})
