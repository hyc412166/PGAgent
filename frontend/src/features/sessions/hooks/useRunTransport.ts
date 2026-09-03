import { useCallback } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { api, apiUrl } from '../../../api'
import { appendAssistantDelta, hasPersistedRunReply, isResumableWaitingRun, isTerminalRunStatus, isTerminalRunStreamEvent, parseRunStreamEvent, rememberRunStreamEvent, runStreamPhase } from '../../../sessionStream'
import type { RunStreamEvent } from '../../../sessionStream'
import { emptyThoughtTimeline, hasVisibleCompletedThought, thinkingStatusForRun, updateThoughtTimeline } from '../../../thoughtTimeline'
import type { ThoughtTimelineState } from '../../../thoughtTimeline'
import type { Approval, Run, RunEvent } from '../../../types'
import { emptyLiveRun, runStreamEventNames } from '../sessionState'
import type { LiveRunState, OwnedSessionDelegations, OwnedSessionMessages, OwnedSessionRuns } from '../sessionState'

type MutableRef<T> = { current: T }
type LoadState<T> = { data: T; loading: boolean; error: string }

type RunTransportOptions = {
  activeIdRef: MutableRef<string>
  eventSourceRef: MutableRef<EventSource | null>
  fallbackTimerRef: MutableRef<number | null>
  streamReconnectTimerRef: MutableRef<number | null>
  streamErrorCountRef: MutableRef<number>
  streamRunIdRef: MutableRef<string>
  seenStreamEventsRef: MutableRef<{ runId: string; eventIds: Set<string> }>
  terminalSyncVersionRef: MutableRef<number>
  liveRunRef: MutableRef<LiveRunState>
  setRunsState: (next: LoadState<OwnedSessionRuns>) => void
  setApprovalsState: (next: LoadState<Approval[]>) => void
  setInterruptedRunId: Dispatch<SetStateAction<string>>
  setCompletedThoughtsByRun: Dispatch<SetStateAction<Record<string, ThoughtTimelineState>>>
  setLiveRun: Dispatch<SetStateAction<LiveRunState>>
  setActionError: Dispatch<SetStateAction<string>>
  setChildPanelOpen: Dispatch<SetStateAction<boolean>>
  refreshMessages: () => Promise<OwnedSessionMessages | undefined>
  refreshRuns: () => Promise<OwnedSessionRuns | undefined>
  refreshContext: () => Promise<unknown>
  refreshChildTasks: () => Promise<OwnedSessionDelegations | undefined>
  refreshTeammates: () => Promise<unknown>
  refreshDurableTask: () => Promise<unknown>
}

export function useRunTransport(options: RunTransportOptions) {
  const {
    activeIdRef, eventSourceRef, fallbackTimerRef, streamReconnectTimerRef, streamErrorCountRef,
    streamRunIdRef, seenStreamEventsRef, terminalSyncVersionRef, liveRunRef, setRunsState,
    setApprovalsState, setInterruptedRunId, setCompletedThoughtsByRun, setLiveRun, setActionError,
    setChildPanelOpen, refreshMessages, refreshRuns, refreshContext, refreshChildTasks,
    refreshTeammates, refreshDurableTask,
  } = options

  const closeRunTransport = useCallback(() => {
    eventSourceRef.current?.close()
    eventSourceRef.current = null
    if (fallbackTimerRef.current !== null) {
      window.clearInterval(fallbackTimerRef.current)
      fallbackTimerRef.current = null
    }
    if (streamReconnectTimerRef.current !== null) {
      window.clearTimeout(streamReconnectTimerRef.current)
      streamReconnectTimerRef.current = null
    }
    streamErrorCountRef.current = 0
    streamRunIdRef.current = ''
  }, [eventSourceRef, fallbackTimerRef, streamErrorCountRef, streamReconnectTimerRef, streamRunIdRef])

  const refreshApprovalsForSession = useCallback(async (sessionId: string) => {
    if (!sessionId || activeIdRef.current !== sessionId) return undefined
    try {
      const latestRuns = await api.list<Run>(`/api/runs?session_id=${encodeURIComponent(sessionId)}`, ['runs'])
      const runIds = latestRuns
        .filter((run) => run.status === 'awaiting_approval')
        .map((run) => run.id)
      const groups = await Promise.all(runIds.map((runId) => api.list<Approval>(
        `/api/approvals?run_id=${encodeURIComponent(runId)}&status=pending`,
        ['approvals'],
      )))
      if (activeIdRef.current !== sessionId) return undefined
      const data = groups.flat()
      // The approval rows and the run status must advance together.  Without
      // this state update, visibleApprovals still filters against the previous
      // run list until the user changes tabs.
      setRunsState({ data: { ownerSessionId: sessionId, items: latestRuns }, loading: false, error: '' })
      setApprovalsState({ data, loading: false, error: '' })
      return data
    } catch {
      return undefined
    }
  }, [activeIdRef, setApprovalsState, setRunsState])

  const syncTerminalRun = useCallback(async (runId: string, event?: RunStreamEvent, sessionId = activeIdRef.current) => {
    const syncVersion = ++terminalSyncVersionRef.current
    closeRunTransport()
    let resolvedEvent = event
    // When SSE was unavailable, the polling fallback only knows the terminal
    // Run status. Recover the persisted interruption payload so a stopped run
    // still exposes its partial draft for the edit action.
    if (event?.type === 'run_state' && event.status === 'stopped' && !event.partial_output) {
      try {
        const persistedEvents = await api.list<RunEvent>(`/api/runs/${encodeURIComponent(runId)}/events`, ['events'])
        const interruption = [...persistedEvents].reverse().find((item) => {
          const type = item.type || item.event_type
          return type === 'run_interrupted'
            && typeof item.payload?.partial_output === 'string'
        })
        const payload = interruption?.payload
        if (payload && typeof payload.partial_output === 'string') {
          resolvedEvent = { ...event, partial_output: payload.partial_output, partial_thought: payload.partial_thought }
        }
      } catch {
        // The regular Run status/message refresh below remains authoritative.
      }
    }
    const stoppedTerminal = resolvedEvent?.type === 'run_interrupted' || resolvedEvent?.type === 'run_stopped'
      || (resolvedEvent?.type === 'run_state' && resolvedEvent.status === 'stopped')
    const userInterruptedTerminal = stoppedTerminal && (
      resolvedEvent?.type === 'run_interrupted'
        ? ['user_interrupted', 'parent_user_interrupted'].includes(String(resolvedEvent?.reason || ''))
        : ['user_interrupted', 'parent_user_interrupted'].includes(String(resolvedEvent?.code || resolvedEvent?.stop_reason || resolvedEvent?.reason || ''))
    )
    if (isTerminalRunStreamEvent(resolvedEvent || { type: '' })) {
      setInterruptedRunId((current) => (
        userInterruptedTerminal ? runId : current === runId ? '' : current
      ))
    }
    const terminalThought = resolvedEvent
      ? updateThoughtTimeline(liveRunRef.current.thought, resolvedEvent)
      : liveRunRef.current.thought
    if (runId && hasVisibleCompletedThought(terminalThought)) {
      // Commit the live timeline at the terminal boundary itself. This avoids
      // depending on a later React effect that can be overtaken by the next
      // turn or a session navigation.
      setCompletedThoughtsByRun((current) => ({ ...current, [runId]: terminalThought }))
    }
    setLiveRun((previous) => ({
      ...previous,
      runId,
      phase: resolvedEvent ? runStreamPhase(resolvedEvent) : previous.phase,
      status: 'terminal',
      error: resolvedEvent?.error ? String(resolvedEvent.error) : previous.error,
      draft: resolvedEvent ? appendAssistantDelta(previous.draft, resolvedEvent) : previous.draft,
      thought: resolvedEvent ? updateThoughtTimeline(previous.thought, resolvedEvent) : previous.thought,
    }))

    let refreshedMessages: OwnedSessionMessages | undefined
    for (const delay of [220, 520, 900]) {
      if (delay > 220) await new Promise((resolve) => window.setTimeout(resolve, delay))
      if (syncVersion !== terminalSyncVersionRef.current || activeIdRef.current !== sessionId) return
      refreshedMessages = await refreshMessages()
      const hasReply = refreshedMessages?.ownerSessionId === sessionId
        && hasPersistedRunReply(refreshedMessages.items, runId)
      if (hasReply) break
    }
    if (syncVersion !== terminalSyncVersionRef.current || activeIdRef.current !== sessionId) return

    await Promise.all([refreshRuns(), refreshContext(), refreshChildTasks(), refreshTeammates(), refreshDurableTask(), refreshApprovalsForSession(sessionId)])
    if (syncVersion !== terminalSyncVersionRef.current || activeIdRef.current !== sessionId) return

    const hasPersistedReply = Boolean(
      refreshedMessages?.ownerSessionId === sessionId
      && hasPersistedRunReply(refreshedMessages.items, runId),
    )
    const hasDraft = Boolean(liveRunRef.current.runId === runId && liveRunRef.current.draft)
    if (resolvedEvent?.error) setActionError(`运行失败：${String(resolvedEvent.error)}`)
    if (hasPersistedReply || (!hasDraft && !userInterruptedTerminal)) {
      setLiveRun(emptyLiveRun())
    }
  }, [
    activeIdRef, closeRunTransport, liveRunRef, refreshApprovalsForSession, refreshChildTasks,
    refreshContext, refreshDurableTask, refreshMessages, refreshRuns, refreshTeammates,
    setActionError, setCompletedThoughtsByRun, setInterruptedRunId, setLiveRun,
    terminalSyncVersionRef,
  ])

  const startRunFallback = useCallback((runId: string, sessionId = activeIdRef.current) => {
    eventSourceRef.current?.close()
    eventSourceRef.current = null
    if (fallbackTimerRef.current !== null) window.clearInterval(fallbackTimerRef.current)
    if (streamReconnectTimerRef.current !== null) {
      window.clearTimeout(streamReconnectTimerRef.current)
      streamReconnectTimerRef.current = null
    }
    streamRunIdRef.current = runId
    setLiveRun((previous) => ({
      ...previous,
      runId,
      phase: previous.phase || '连接中断，正在同步…',
      status: 'fallback',
      error: '',
    }))

    let polling = false
    const poll = async () => {
      if (polling || streamRunIdRef.current !== runId || activeIdRef.current !== sessionId) return
      polling = true
      try {
        let run: Run | undefined
        try {
          run = await api.get<Run>(`/api/runs/${encodeURIComponent(runId)}`)
        } catch {
          const latestRuns = await refreshRuns()
          run = latestRuns?.ownerSessionId === sessionId
            ? latestRuns.items.find((item) => item.id === runId)
            : undefined
        }
        if (!run || streamRunIdRef.current !== runId || activeIdRef.current !== sessionId) return
        const status = run.status || ''
        const resumableWaiting = isResumableWaitingRun(run)
        setLiveRun((previous) => ({
          ...previous,
          runId,
          phase: runStreamPhase({ type: 'run_state', status, reason: run.stop_reason }),
          status: status === 'awaiting_approval' ? 'awaiting_approval' : isTerminalRunStatus(status) && !resumableWaiting ? 'terminal' : 'fallback',
          error: '',
        }))
        void refreshRuns()
        if (status === 'awaiting_approval') void refreshApprovalsForSession(sessionId)
        if (isTerminalRunStatus(status) && !resumableWaiting) {
          await syncTerminalRun(runId, { type: 'run_state', status, terminal: true, error: status === 'failed' ? (run.error_message || run.stop_reason) : undefined, reason: run.stop_reason }, sessionId)
        }
      } finally {
        polling = false
      }
    }

    void poll()
    fallbackTimerRef.current = window.setInterval(() => void poll(), 4000)
  }, [
    activeIdRef, eventSourceRef, fallbackTimerRef, refreshApprovalsForSession, refreshRuns,
    setLiveRun, streamReconnectTimerRef, streamRunIdRef, syncTerminalRun,
  ])

  const startRunStream = useCallback((runId: string, sessionId = activeIdRef.current) => {
    if (!runId || activeIdRef.current !== sessionId) return
    if (streamRunIdRef.current === runId && eventSourceRef.current) return
    eventSourceRef.current?.close()
    eventSourceRef.current = null
    if (fallbackTimerRef.current !== null) {
      window.clearInterval(fallbackTimerRef.current)
      fallbackTimerRef.current = null
    }
    if (streamReconnectTimerRef.current !== null) {
      window.clearTimeout(streamReconnectTimerRef.current)
      streamReconnectTimerRef.current = null
    }
    streamErrorCountRef.current = 0
    streamRunIdRef.current = runId
    if (seenStreamEventsRef.current.runId !== runId) {
      seenStreamEventsRef.current = { runId, eventIds: new Set() }
    }
    setLiveRun((previous) => ({
      runId,
      phase: previous.runId === runId && previous.phase ? previous.phase : '思考中…',
      draft: previous.runId === runId ? previous.draft : '',
      status: 'connecting',
      error: '',
      thought: previous.runId === runId ? previous.thought : { ...emptyThoughtTimeline, startedAt: Date.now() },
      thinkingStatus: previous.thinkingStatus || thinkingStatusForRun(runId),
    }))

    let source: EventSource
    try {
      source = new EventSource(apiUrl(`/api/runs/${encodeURIComponent(runId)}/stream`))
    } catch {
      startRunFallback(runId, sessionId)
      return
    }
    eventSourceRef.current = source

    source.onopen = () => {
      if (eventSourceRef.current !== source || activeIdRef.current !== sessionId) return
      streamErrorCountRef.current = 0
      if (streamReconnectTimerRef.current !== null) {
        window.clearTimeout(streamReconnectTimerRef.current)
        streamReconnectTimerRef.current = null
      }
      setLiveRun((previous) => ({ ...previous, status: previous.status === 'awaiting_approval' ? 'awaiting_approval' : 'live', error: '' }))
    }

    const handleEvent = (message: MessageEvent<string>, fallbackType = '') => {
      if (eventSourceRef.current !== source || activeIdRef.current !== sessionId) return
      const parsed = parseRunStreamEvent(message.data, fallbackType)
      if (!parsed) return
      if (!rememberRunStreamEvent(seenStreamEventsRef.current.eventIds, parsed, message.lastEventId)) return
      const terminal = isTerminalRunStreamEvent(parsed)
      const delegatedChildEvent = parsed.type.startsWith('delegated_child_')
      const waitingApproval = parsed.type === 'approval_requested'
        || parsed.type === 'delegated_child_awaiting_approval'
        || (parsed.type === 'run_state' && parsed.status === 'awaiting_approval')
      setLiveRun((previous) => ({
        ...previous,
        runId,
        phase: runStreamPhase(parsed),
        // Text received before a tool call is model progress, not the final
        // answer.  Move it to the safe thought summary and clear the live
        // answer draft as soon as the tool turn actually starts.
        draft: parsed.type === 'tool_started' || parsed.type === 'tool_call'
          ? ''
          : appendAssistantDelta(previous.draft, parsed),
        status: terminal ? 'terminal' : waitingApproval ? 'awaiting_approval' : 'live',
        error: parsed.error ? String(parsed.error) : previous.error,
        thought: updateThoughtTimeline(previous.thought, parsed),
      }))
      if (waitingApproval) void refreshApprovalsForSession(sessionId)
      if (parsed.type === 'tool_finished' && ['update_plan', 'todowrite', 'TodoWrite'].includes(String(parsed.tool_name || ''))) {
        void refreshDurableTask()
      }
      if (delegatedChildEvent) {
        void refreshRuns()
        void refreshChildTasks()
        void refreshTeammates()
        setChildPanelOpen(true)
      }
      if (terminal) void syncTerminalRun(runId, parsed, sessionId)
    }

    source.onmessage = (message) => handleEvent(message)
    for (const eventName of runStreamEventNames) {
      source.addEventListener(eventName, ((event: MessageEvent<string>) => handleEvent(event, eventName)) as EventListener)
    }
    source.onerror = () => {
      if (eventSourceRef.current !== source || activeIdRef.current !== sessionId) return
      streamErrorCountRef.current += 1
      setLiveRun((previous) => ({ ...previous, phase: previous.phase || '连接中断，正在重连…', status: 'connecting' }))
      if (streamReconnectTimerRef.current !== null) return
      streamReconnectTimerRef.current = window.setTimeout(() => {
        streamReconnectTimerRef.current = null
        if (eventSourceRef.current !== source || activeIdRef.current !== sessionId || source.readyState === EventSource.OPEN) return
        source.close()
        eventSourceRef.current = null
        startRunFallback(runId, sessionId)
      }, streamErrorCountRef.current >= 3 ? 3500 : 6500)
    }
  }, [
    activeIdRef, eventSourceRef, fallbackTimerRef, refreshApprovalsForSession, refreshChildTasks,
    refreshDurableTask, refreshRuns, refreshTeammates, seenStreamEventsRef, setChildPanelOpen,
    setLiveRun, startRunFallback, streamErrorCountRef, streamReconnectTimerRef, streamRunIdRef,
    syncTerminalRun,
  ])


  return { closeRunTransport, refreshApprovalsForSession, startRunStream }
}
