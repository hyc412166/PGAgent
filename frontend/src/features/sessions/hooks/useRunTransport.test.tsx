import { createElement } from 'react'
import type { SetStateAction } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api'
import { emptyLiveRun } from '../sessionState'
import type { LiveRunState } from '../sessionState'
import { useRunTransport } from './useRunTransport'

class Stream {
  static OPEN = 1
  static latest: Stream
  readyState = 1
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  listeners = new Map<string, (event: MessageEvent<string>) => void>()
  sequence = 0
  constructor() { Stream.latest = this }
  close() { this.readyState = 2 }
  addEventListener(name: string, listener: (event: MessageEvent<string>) => void) { this.listeners.set(name, listener) }
  emit(type: string, count?: number, extra: Record<string, unknown> = {}) {
    this.sequence += 1
    this.listeners.get(type)?.({ data: JSON.stringify({ type, event_id: String(this.sequence), active_context_tokens: count, ...extra }) } as MessageEvent<string>)
  }
}

function setup(historyLoader?: typeof api.listRunEvents, persistedStartedAt?: string) {
  vi.useFakeTimers()
  vi.stubGlobal('window', globalThis)
  vi.stubGlobal('EventSource', Stream)
  vi.spyOn(api, 'listRunEvents').mockImplementation(historyLoader ?? (async () => ({ items: [], next_before: null })))
  const activeIdRef = { current: 'a' }
  const refreshContext = vi.fn().mockResolvedValue(undefined)
  const refresh = vi.fn().mockResolvedValue(undefined)
  const liveRunRef = { current: emptyLiveRun() }
  let transport!: ReturnType<typeof useRunTransport>
  function Harness() {
    transport = useRunTransport({
      activeIdRef, eventSourceRef: { current: null }, fallbackTimerRef: { current: null },
      streamReconnectTimerRef: { current: null }, streamErrorCountRef: { current: 0 },
      streamRunIdRef: { current: '' }, seenStreamEventsRef: { current: { runId: '', eventIds: new Set() } },
      terminalSyncVersionRef: { current: 0 }, liveRunRef,
      setRunsState: vi.fn(), setApprovalsState: vi.fn(), setInterruptedRunId: vi.fn(),
      setCompletedThoughtsByRun: vi.fn(), setLiveRun: (update: SetStateAction<LiveRunState>) => {
        liveRunRef.current = typeof update === 'function' ? update(liveRunRef.current) : update
      }, setActionError: vi.fn(), setChildPanelOpen: vi.fn(),
      refreshMessages: refresh, refreshRuns: refresh, refreshContext, refreshChildTasks: refresh,
      refreshTeammates: refresh, refreshDurableTask: refresh,
    })
    return null
  }
  renderToString(createElement(Harness))
  transport.startRunStream('run-a', 'a', persistedStartedAt)
  return { transport, activeIdRef, refreshContext, source: Stream.latest, liveRunRef }
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

describe('运行中上下文刷新', () => {
  it('步骤与工具事件触发刷新，合并回放事件而不等待终态', async () => {
    const { source, refreshContext } = setup()
    source.emit('context_prepared', 100)
    source.emit('context_usage_updated', 300)
    source.emit('context_usage_updated', 400)
    await vi.advanceTimersByTimeAsync(150)
    expect(refreshContext).toHaveBeenCalledTimes(1)
    source.emit('context_compaction_finished', 80)
    await vi.advanceTimersByTimeAsync(150)
    expect(refreshContext).toHaveBeenCalledTimes(2)
    source.emit('assistant_message_delta')
    await vi.advanceTimersByTimeAsync(150)
    expect(refreshContext).toHaveBeenCalledTimes(2)
  })

  it('切换会话或关闭流后不执行旧会话的延迟刷新', async () => {
    const { source, refreshContext, activeIdRef, transport } = setup()
    source.emit('tool_finished', 300)
    activeIdRef.current = 'b'
    transport.closeRunTransport()
    await vi.advanceTimersByTimeAsync(150)
    expect(refreshContext).not.toHaveBeenCalled()
  })

  it('重新连接时即使历史事件已去重，仍读取最新上下文', async () => {
    const { source, refreshContext } = setup()
    source.onopen?.()
    await vi.advanceTimersByTimeAsync(150)
    expect(refreshContext).toHaveBeenCalledTimes(1)
  })

  it('历史分页晚到时补齐第一次压缩，保留实时第二次压缩及等待计时', async () => {
    let resolvePage!: (value: Awaited<ReturnType<typeof api.listRunEvents>>) => void
    const { source, liveRunRef } = setup(() => new Promise(resolve => { resolvePage = resolve }), '2026-10-14T12:00:00Z')
    source.emit('context_compaction_started', undefined, { id: 'second', created_at: '2026-10-14T12:02:00Z' })
    source.emit('context_compaction_finished', 800, { id: 'second-done', created_at: '2026-10-14T12:02:10Z' })
    resolvePage({ items: [
      { id: 'step', event_type: 'model_step_started', sequence: 1, payload: { step: 1 }, created_at: '2026-10-14T12:00:00Z' },
      { id: 'first', event_type: 'context_compaction_started', sequence: 2, created_at: '2026-10-14T12:00:10Z' },
      { id: 'first-done', event_type: 'context_compaction_finished', sequence: 3, created_at: '2026-10-14T12:00:20Z' },
      { id: 'second', event_type: 'context_compaction_started', sequence: 4, created_at: '2026-10-14T12:02:00Z' },
    ], next_before: null })
    await vi.advanceTimersByTimeAsync(150)
    const compressions = liveRunRef.current.thought.items.filter(item => item.icon === 'context' && item.kind === 'event')
    expect(compressions.map(item => item.status)).toEqual(['completed', 'completed'])
    expect(liveRunRef.current.thought.activeStepStartedAt).toBe(Date.parse('2026-10-14T12:02:10Z'))
    expect(liveRunRef.current.thought.startedAt).toBe(Date.parse('2026-10-14T12:00:00Z'))
  })

  it('历史先恢复后收到同一压缩的 SSE 重放，不把已完成记录变回运行中', async () => {
    const { source, liveRunRef } = setup(async () => ({ items: [
      { id: 'first', event_type: 'context_compaction_started', sequence: 1, created_at: '2026-10-14T12:00:10Z' },
      { id: 'first-done', event_type: 'context_compaction_finished', sequence: 2, created_at: '2026-10-14T12:00:20Z' },
    ], next_before: null }), '2026-10-14T12:00:00Z')
    await vi.advanceTimersByTimeAsync(150)
    source.emit('context_compaction_started', undefined, { id: 'first', created_at: '2026-10-14T12:00:10Z' })
    expect(liveRunRef.current.thought.items.filter(item => item.kind === 'event').map(item => item.status)).toEqual(['completed'])
  })

  it('SSE 仅收到压缩完成时，迟到历史补齐开始事件后仍只显示一条记录', async () => {
    let resolvePage!: (value: Awaited<ReturnType<typeof api.listRunEvents>>) => void
    const { source, liveRunRef } = setup(() => new Promise(resolve => { resolvePage = resolve }), '2026-10-14T12:00:00Z')
    source.emit('context_compaction_finished', 800, { id: 'first-done', created_at: '2026-10-14T12:00:20Z' })
    resolvePage({ items: [
      { id: 'first', event_type: 'context_compaction_started', sequence: 1, created_at: '2026-10-14T12:00:10Z' },
      { id: 'first-done', event_type: 'context_compaction_finished', sequence: 2, created_at: '2026-10-14T12:00:20Z' },
    ], next_before: null })
    await vi.advanceTimersByTimeAsync(150)
    expect(liveRunRef.current.thought.items.filter(item => item.kind === 'event').map(item => item.status)).toEqual(['completed'])
    expect(liveRunRef.current.thought.activeStepStartedAt).toBe(Date.parse('2026-10-14T12:00:20Z'))
  })

  it.each(['context_compaction_finished', 'context_compaction_failed'])('历史中的 %s 不被实时旧开始事件覆盖', async (endType) => {
    let resolvePage!: (value: Awaited<ReturnType<typeof api.listRunEvents>>) => void
    const { source, liveRunRef } = setup(() => new Promise(resolve => { resolvePage = resolve }), '2026-10-14T12:00:00Z')
    source.emit('model_step_started', undefined, { id: 'step', step: 1, created_at: '2026-10-14T12:00:00Z' })
    source.emit('context_compaction_started', undefined, { id: 'first', created_at: '2026-10-14T12:00:10Z' })
    resolvePage({ items: [
      { id: 'first', event_type: 'context_compaction_started', sequence: 1, created_at: '2026-10-14T12:00:10Z' },
      { id: 'first-done', event_type: endType, sequence: 2, created_at: '2026-10-14T12:00:20Z' },
    ], next_before: null })
    await vi.advanceTimersByTimeAsync(150)
    expect(liveRunRef.current.thought.items.filter(item => item.kind === 'event').map(item => item.status)).toEqual([endType.endsWith('failed') ? 'failed' : 'completed'])
  })
})
