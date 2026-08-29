import { AlertCircle, ArrowUp, BookOpen, Cable, Check, ChevronRight, Folder, FolderOpen, LoaderCircle, MessageSquare, PanelRightClose, PanelRightOpen, Pencil, Plus, ShieldCheck, Square, Trash2, X } from 'lucide-react'
import { Fragment, type FormEvent, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, describeError } from '../../api'
import { permissionLabel, permissionOptions, toggleSelectedId } from '../../capabilitySelection'
import { modelSelectionPayload, resolveEffectiveThinking, shortModelLabel, thinkingLevelLabels } from '../../composerSettings'
import { buildDraftLaunchPayload, createDraftIdempotencyKey, createTurnIdempotencyKey } from '../../draftLaunch'
import { availableConnectionModels, resolveEffectiveModelSettings } from '../../modelSettings'
import { buildSessionNavigation, folderName, isDefaultWorkspace, projectRootForSession } from '../../sessionNavigation'
import { isResumableWaitingRun, isTerminalRunStatus, shouldRefreshConversationAfterApprovalDecision, shouldShowStoppedRunNotice, shouldStartHistoryScroll, visibleSessionItems } from '../../sessionStream'
import { emptyThoughtTimeline, hasVisibleCompletedThought, pickThinkingStatus, timelineFromRunEvents } from '../../thoughtTimeline'
import type { ThoughtTimelineState } from '../../thoughtTimeline'
import { ThoughtHydrationRegistry } from '../../thoughtHydration'
import type { ThoughtHydrationToken } from '../../thoughtHydration'
import { ContextUsageRing, EmptyState, ErrorState, LoadingState } from '../../components/ui'
import { ApprovalCard, ChildAgentPanel, CompletedThoughtTimeline, LiveAssistantMessage, MessageBubble } from './presentation'
import { useApiData } from '../../shared/hooks/useApiData'
import { stringId } from '../../shared/lib/display'
import { ComposerTextArea } from './components/ComposerTextArea'
import type { ComposerTextAreaHandle } from './components/ComposerTextArea'
import { DurableTaskCard } from './components/DurableTaskCard'
import { useRunTransport } from './hooks/useRunTransport'
import { activeRunStatuses, emptyDraftContext, emptyDraftSettings, emptyLiveRun, noDelegatedTasks, noTeammates } from './sessionState'
import type { DraftLaunchResponse, DraftSessionSettings, LiveRunState, OwnedSessionDelegations, OwnedSessionMessages, OwnedSessionRuns, ProjectHoverCard } from './sessionState'
import type { AgentProfile, Approval, Connection, DelegatedTask, DurableTask, FolderSelection, McpServer, MemorySettings, Message, PermissionMode, Run, RunEvent, Session, SessionContext, SkillCatalogItem, Teammate, ThinkingLevel, Workspace } from '../../types'

function SessionsPage() {
  const sessions = useApiData<Session[]>([], () => api.list<Session>('/api/sessions', ['sessions']), [])
  const agents = useApiData<AgentProfile[]>([], () => api.list<AgentProfile>('/api/agents', ['agents']), [])
  const workspaces = useApiData<Workspace[]>([], () => api.list<Workspace>('/api/workspaces', ['workspaces']), [])
  const connections = useApiData<Connection[]>([], () => api.list<Connection>('/api/connections', ['connections']), [])
  const skills = useApiData<SkillCatalogItem[]>([], () => api.list<SkillCatalogItem>('/api/skills', ['skills']), [])
  const mcpServers = useApiData<McpServer[]>([], () => api.list<McpServer>('/api/mcp/servers', ['mcp-servers']), [])
  const memorySettings = useApiData<MemorySettings | null>(null, () => api.get<MemorySettings>('/api/memories/settings'), [])
  const [activeId, setActiveId] = useState('')
  const [composerHasValue, setComposerHasValue] = useState(false)
  const [sending, setSending] = useState(false)
  const [stoppingRunId, setStoppingRunId] = useState('')
  const [cancellingTaskId, setCancellingTaskId] = useState('')
  const [deletingSessionId, setDeletingSessionId] = useState('')
  const [interruptedRunId, setInterruptedRunId] = useState('')
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [settingsMenuOpen, setSettingsMenuOpen] = useState(false)
  const [settingsSubmenu, setSettingsSubmenu] = useState<'model' | 'thinking' | null>(null)
  const [addMenuOpen, setAddMenuOpen] = useState(false)
  const [skillSubmenuOpen, setSkillSubmenuOpen] = useState(false)
  const [mcpSubmenuOpen, setMcpSubmenuOpen] = useState(false)
  const [permissionMenuOpen, setPermissionMenuOpen] = useState(false)
  const [capabilitySaving, setCapabilitySaving] = useState(false)
  const settingsMenuRef = useRef<HTMLDivElement>(null)
  const settingsTriggerRef = useRef<HTMLButtonElement>(null)
  const modelSubmenuRef = useRef<HTMLDivElement>(null)
  const thinkingSubmenuRef = useRef<HTMLDivElement>(null)
  const addMenuRef = useRef<HTMLDivElement>(null)
  const permissionMenuRef = useRef<HTMLDivElement>(null)
  const [decidingApproval, setDecidingApproval] = useState('')
  const [actionError, setActionError] = useState('')
  const [draftActive, setDraftActive] = useState(false)
  const [draftRootPath, setDraftRootPath] = useState('')
  const [draftSettings, setDraftSettings] = useState<DraftSessionSettings>(emptyDraftSettings)
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<Set<string>>(() => new Set())
  const [addingProject, setAddingProject] = useState(false)
  const [pickingDraftProject, setPickingDraftProject] = useState(false)
  const [projectError, setProjectError] = useState('')
  const [projectHoverCard, setProjectHoverCard] = useState<ProjectHoverCard | null>(null)
  const [completedThoughtsByRun, setCompletedThoughtsByRun] = useState<Record<string, ThoughtTimelineState>>({})
  const [childPanelOpen, setChildPanelOpen] = useState(false)
  const [selectedChildTaskId, setSelectedChildTaskId] = useState('')
  const childPanelAutoOpenedRef = useRef(false)
  const [liveRun, setLiveRun] = useState<LiveRunState>(emptyLiveRun)
  const eventSourceRef = useRef<EventSource | null>(null)
  const fallbackTimerRef = useRef<number | null>(null)
  const streamReconnectTimerRef = useRef<number | null>(null)
  const streamErrorCountRef = useRef(0)
  const streamRunIdRef = useRef('')
  const seenStreamEventsRef = useRef<{ runId: string; eventIds: Set<string> }>({ runId: '', eventIds: new Set() })
  const messagesRef = useRef<HTMLDivElement>(null)
  const composerInputRef = useRef<ComposerTextAreaHandle>(null)
  const activeIdRef = useRef('')
  const stickToBottomRef = useRef(true)
  const historyScrollSessionRef = useRef('')
  const terminalSyncVersionRef = useRef(0)
  const pendingDraftRunRef = useRef<{ sessionId: string; runId: string } | null>(null)
  const draftIdempotencyKeyRef = useRef('')
  const pendingSessionSendRef = useRef<{ sessionId: string; content: string; key: string } | null>(null)
  const draftVersionRef = useRef(0)
  const sendingRef = useRef(false)
  const lastSubmittedContentRef = useRef('')
  const thoughtHydrationRegistryRef = useRef(new ThoughtHydrationRegistry())
  const [historyHydration, setHistoryHydration] = useState({ sessionId: '', complete: false })
  const [historyOpenVersion, setHistoryOpenVersion] = useState(0)
  activeIdRef.current = activeId
  const liveRunRef = useRef(liveRun)
  liveRunRef.current = liveRun

  useEffect(() => {
    if (!activeId && !draftActive && sessions.data[0]) setActiveId(stringId(sessions.data[0].id))
  }, [activeId, draftActive, sessions.data])

  useEffect(() => {
    if (!settingsMenuOpen) return
    const closeOnPointerDown = (event: PointerEvent) => {
      if (!settingsMenuRef.current?.contains(event.target as Node)) {
        setSettingsMenuOpen(false); setSettingsSubmenu(null)
      }
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSettingsMenuOpen(false); setSettingsSubmenu(null)
        window.requestAnimationFrame(() => settingsTriggerRef.current?.focus())
      }
    }
    document.addEventListener('pointerdown', closeOnPointerDown)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnPointerDown)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [settingsMenuOpen])

  useEffect(() => {
    if (!addMenuOpen && !permissionMenuOpen) return
    const closeOnPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (addMenuOpen && !addMenuRef.current?.contains(target)) {
        setAddMenuOpen(false)
        setSkillSubmenuOpen(false)
        setMcpSubmenuOpen(false)
      }
      if (permissionMenuOpen && !permissionMenuRef.current?.contains(target)) setPermissionMenuOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setAddMenuOpen(false)
      setSkillSubmenuOpen(false)
      setMcpSubmenuOpen(false)
      setPermissionMenuOpen(false)
    }
    document.addEventListener('pointerdown', closeOnPointerDown)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnPointerDown)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [addMenuOpen, permissionMenuOpen])

  useEffect(() => {
    setSettingsMenuOpen(false); setSettingsSubmenu(null)
    setAddMenuOpen(false); setSkillSubmenuOpen(false); setMcpSubmenuOpen(false); setPermissionMenuOpen(false)
    setSelectedChildTaskId('')
  }, [activeId])

  const messages = useApiData<OwnedSessionMessages>(
    { ownerSessionId: '', items: [] },
    async () => activeId
      ? { ownerSessionId: activeId, items: await api.list<Message>(`/api/sessions/${activeId}/messages`, ['messages']) }
      : { ownerSessionId: '', items: [] },
    [activeId],
  )
  const runs = useApiData<OwnedSessionRuns>(
    { ownerSessionId: '', items: [] },
    async () => activeId
      ? { ownerSessionId: activeId, items: await api.list<Run>(`/api/runs?session_id=${encodeURIComponent(activeId)}`, ['runs']) }
      : { ownerSessionId: '', items: [] },
    [activeId],
  )
  const childTasks = useApiData<OwnedSessionDelegations>({ ownerSessionId: '', items: [] }, async () => {
    if (!activeId) return { ownerSessionId: '', items: [] }
    return {
      ownerSessionId: activeId,
      items: await api.list<DelegatedTask>(`/api/sessions/${encodeURIComponent(activeId)}/delegations`, ['delegations']),
    }
  }, [activeId])
  const teammates = useApiData<Teammate[]>([], () => activeId
    ? api.list<Teammate>(`/api/sessions/${encodeURIComponent(activeId)}/teammates`, ['teammates'])
    : Promise.resolve([]), [activeId])
  const durableTask = useApiData<DurableTask | null>(null, () => activeId
    ? api.get<DurableTask | null>(`/api/sessions/${encodeURIComponent(activeId)}/active-task`)
    : Promise.resolve(null), [activeId])
  const refreshDurableTask = durableTask.refresh
  const context = useApiData<SessionContext | null>(null, () => activeId ? api.get<SessionContext>(`/api/sessions/${activeId}/context`) : Promise.resolve(null), [activeId])
  const activeSession = sessions.data.find((item) => stringId(item.id) === activeId)
  const activeAgent = agents.data.find((item) => item.id === activeSession?.agent_id)
  const sessionRuns = visibleSessionItems(runs.data.ownerSessionId, activeId, runs.data.items)
    .filter((item) => !item.session_id || item.session_id === activeId)
  const awaitingApprovalRunIds = sessionRuns
    .filter((item) => item.status === 'awaiting_approval')
    .map((item) => item.id)
  const approvalRunIdsKey = awaitingApprovalRunIds.join(',')
  // A child awaiting approval takes precedence over its parent so the card is
  // actionable in this very conversation instead of hidden behind a parent
  // run that has already stopped for the child.
  const activeRun = sessionRuns.find((item) => item.status === 'awaiting_approval')
    ?? sessionRuns.find((item) => isResumableWaitingRun(item))
    ?? sessionRuns.find((item) => activeRunStatuses.has(item.status || ''))
    ?? sessionRuns[0]
  const activeRunId = activeRun?.id || ''
  const activeRunCanStream = Boolean(activeRun && (
    activeRunStatuses.has(activeRun.status || '') || isResumableWaitingRun(activeRun)
  ))
  // `sending` only covers the initial launch request. Once the request has
  // returned, the run is still active while its SSE stream/fallback polling
  // is working. Keep the composer action bound to that run so users can
  // interrupt the model at any point instead of seeing a disabled send icon.
  const liveRunIsActive = Boolean(
    liveRun.runId
      && (
        ['connecting', 'live', 'fallback', 'awaiting_approval'].includes(liveRun.status)
        || sessionRuns.some((item) => item.id === liveRun.runId && isResumableWaitingRun(item))
      ),
  )
  const activeRunIsInterruptible = Boolean(
    liveRun.status !== 'terminal'
      && activeRunId
      && activeRunCanStream,
  )
  const interruptibleRunId = liveRunIsActive ? liveRun.runId : activeRunIsInterruptible ? activeRunId : ''
  const canInterrupt = Boolean(interruptibleRunId)
  const canEditInterrupted = Boolean(
    interruptedRunId
      && liveRun.runId === interruptedRunId
      && liveRun.status === 'terminal'
      && lastSubmittedContentRef.current.trim(),
  )
  // Legacy versions wrote raw child responses into the chat transcript. Hide
  // those rows too; the source of truth is now the child side panel.
  const visibleMessages = visibleSessionItems(messages.data.ownerSessionId, activeId, messages.data.items)
    .filter((message) => message.metadata?.delegated_child !== true)
    // Runtime tool turns and delegated terminal observations are durable
    // provider/checkpoint transcript, not user-facing chat bubbles.  They are
    // rendered through the activity timeline and child-agent side panel.
    .filter((message) => message.role !== 'tool')
    .filter((message) => message.metadata?.runtime_run_id == null)
    .filter((message) => message.metadata?.delegated_result_revision !== true)
    .filter((message) => !(message.role === 'assistant' && Array.isArray(message.metadata?.tool_calls)))
  const repliedRunIds = new Set(visibleMessages.flatMap((message) => {
    if (message.role !== 'assistant') return []
    const runId = stringId(message.metadata?.run_id)
    return runId ? [runId] : []
  }))
  const historyMessageRunIds = [...repliedRunIds].join('|')
  const stoppedNoticeHistoryReady = Boolean(activeId)
    && messages.data.ownerSessionId === activeId
    && runs.data.ownerSessionId === activeId
    && !messages.loading
    && !runs.loading
    && !messages.error
    && !runs.error
  const stoppedRunNotices = sessionRuns
    .filter((run) => shouldShowStoppedRunNotice(run, repliedRunIds, stoppedNoticeHistoryReady))
    .slice()
    .reverse()
  const completedThoughtLayoutVersion = Object.entries(completedThoughtsByRun)
    .map(([runId, timeline]) => `${runId}:${timeline.elapsedMs}:${timeline.tools.length}:${timeline.items?.length ?? 0}`)
    .join('|')
  const visibleChildTasks = childTasks.data.ownerSessionId === activeId ? childTasks.data.items : noDelegatedTasks
  const visibleTeammates = activeId ? teammates.data : noTeammates
  const sessionNavigation = buildSessionNavigation(workspaces.data, sessions.data)
  const activeChildTask = visibleChildTasks.find((task) => task.id === selectedChildTaskId) ?? visibleChildTasks[0]
  const childTaskRunId = stringId(activeChildTask?.child_run_id) || stringId(activeChildTask?.result?.child_run_id)
  const childTaskRun = childTaskRunId ? sessionRuns.find((run) => run.id === childTaskRunId) : undefined
  const childTaskEvents = useApiData<RunEvent[]>([], () => childTaskRunId
    ? api.list<RunEvent>(`/api/runs/${encodeURIComponent(childTaskRunId)}/events`, ['events'])
    : Promise.resolve([]), [childTaskRunId])
  const approvals = useApiData<Approval[]>([], async () => {
    const runIds = approvalRunIdsKey ? approvalRunIdsKey.split(',').filter(Boolean) : []
    if (!runIds.length) return []
    const groups = await Promise.all(runIds.map((runId) => api.list<Approval>(
      `/api/approvals?run_id=${encodeURIComponent(runId)}&status=pending`,
      ['approvals'],
    )))
    return groups.flat()
  }, [approvalRunIdsKey])
  const visibleApprovals = approvals.data.filter((approval) => (
    awaitingApprovalRunIds.includes(stringId(approval.run_id))
      || (liveRun.status === 'awaiting_approval' && stringId(approval.run_id) === liveRun.runId)
  ))
  const refreshMessages = messages.refresh
  const refreshRuns = runs.refresh
  const refreshChildTasks = childTasks.refresh
  const refreshTeammates = teammates.refresh
  const refreshContext = context.refresh
  const setRunsState = runs.setState
  const setApprovalsState = approvals.setState
  const settingsLocked = activeRunCanStream || !['idle', 'terminal'].includes(liveRun.status)

  useEffect(() => {
    if (liveRun.status !== 'terminal' || !liveRun.runId || !hasVisibleCompletedThought(liveRun.thought)) return
    setCompletedThoughtsByRun((current) => current[liveRun.runId] === liveRun.thought ? current : { ...current, [liveRun.runId]: liveRun.thought })
  }, [liveRun.runId, liveRun.status, liveRun.thought])

  useLayoutEffect(() => {
    setCompletedThoughtsByRun({})
    setInterruptedRunId('')
    thoughtHydrationRegistryRef.current.reset()
    historyScrollSessionRef.current = activeId
    stickToBottomRef.current = true
    setHistoryHydration({ sessionId: activeId, complete: false })
  }, [activeId])

  useEffect(() => {
    if (!visibleChildTasks.length) {
      childPanelAutoOpenedRef.current = false
      setSelectedChildTaskId('')
      return
    }
    // Opening a history entry with delegated work should reveal its side
    // panel once.  A later manual close is respected until the session has no
    // child tasks (or the user switches sessions).
    if (!childPanelAutoOpenedRef.current) {
      childPanelAutoOpenedRef.current = true
      setChildPanelOpen(true)
    }
    if (!visibleChildTasks.some((task) => task.id === selectedChildTaskId)) {
      setSelectedChildTaskId(visibleChildTasks[0].id)
    }
  }, [selectedChildTaskId, visibleChildTasks])

  useEffect(() => {
    if (!activeId || runs.loading || runs.data.ownerSessionId !== activeId) return
    let cancelled = false
    const finishedRunIds = new Set(
      runs.data.items
        .filter((run) => (!run.session_id || run.session_id === activeId) && isTerminalRunStatus(run.status))
        .map((run) => run.id),
    )
    // Messages are the authoritative history index. `/api/runs` is bounded,
    // so relying only on that list would lose thought timelines in long
    // sessions once the run count exceeds its page limit.
    for (const runId of historyMessageRunIds.split('|')) if (runId) finishedRunIds.add(runId)
    const registry = thoughtHydrationRegistryRef.current
    const runsToHydrate = [...finishedRunIds].filter((runId) => registry.shouldLoad(runId))
    if (!runsToHydrate.length) {
      setHistoryHydration((current) => current.sessionId === activeId && current.complete
        ? current
        : { sessionId: activeId, complete: true })
      return
    }
    setHistoryHydration({ sessionId: activeId, complete: false })
    const activeRequests: Array<{ runId: string; token: ThoughtHydrationToken }> = []
    const requests = runsToHydrate.map(async (runId) => {
      const token = registry.begin(runId)
      if (!token) return
      activeRequests.push({ runId, token })
      try {
        const events = await api.list<RunEvent>(`/api/runs/${encodeURIComponent(runId)}/events`, ['events'])
        if (cancelled || activeIdRef.current !== activeId) return
        const timeline = timelineFromRunEvents(events.map((event) => ({ ...event, type: event.type || event.event_type || '' })))
        if (!registry.complete(runId, token)) return
        if (hasVisibleCompletedThought(timeline)) setCompletedThoughtsByRun((current) => ({ ...current, [runId]: timeline }))
      } catch {
        registry.cancel(runId, token)
      }
    })
    void Promise.all(requests).then(() => {
      if (!cancelled && activeIdRef.current === activeId) setHistoryHydration({ sessionId: activeId, complete: true })
    })
    return () => {
      cancelled = true
      for (const request of activeRequests) registry.cancel(request.runId, request.token)
    }
  }, [activeId, historyMessageRunIds, runs.data.items, runs.data.ownerSessionId, runs.loading])
  useEffect(() => {
    if (settingsLocked || sending) {
      setSettingsMenuOpen(false)
      setSettingsSubmenu(null)
      setAddMenuOpen(false)
      setSkillSubmenuOpen(false)
      setMcpSubmenuOpen(false)
      setPermissionMenuOpen(false)
    }
  }, [sending, settingsLocked])

  useEffect(() => {
    if (draftActive) return
    const workspaceId = activeSession?.workspace_id || ''
    const isProject = workspaces.data.some((workspace) => workspace.id === workspaceId && !isDefaultWorkspace(workspace))
    setExpandedWorkspaceIds(isProject ? new Set([workspaceId]) : new Set())
  }, [activeSession?.workspace_id, draftActive, workspaces.data])

  function clearDraftState() {
    draftVersionRef.current += 1
    setDraftActive(false)
    setDraftRootPath('')
    setDraftSettings(emptyDraftSettings)
    setLiveRun(emptyLiveRun())
    draftIdempotencyKeyRef.current = ''
  }

  function beginDraft() {
    if (sendingRef.current) return
    const selectedProjectRoot = projectRootForSession(workspaces.data, activeSession)
    const selectedProjectId = selectedProjectRoot ? activeSession?.workspace_id : ''
    draftVersionRef.current += 1
    setActionError('')
    setDraftRootPath(selectedProjectRoot)
    setDraftSettings(emptyDraftSettings)
    setLiveRun(emptyLiveRun())
    draftIdempotencyKeyRef.current = createDraftIdempotencyKey()
    setExpandedWorkspaceIds(selectedProjectId ? new Set([selectedProjectId]) : new Set())
    setActiveId('')
    setDraftActive(true)
  }

  function openExistingSession(sessionId: string) {
    if (draftActive && sendingRef.current) return
    if (draftActive) {
      clearDraftState()
    }
    setActionError('')
    stickToBottomRef.current = true
    historyScrollSessionRef.current = sessionId
    setHistoryOpenVersion((version) => version + 1)
    setActiveId(sessionId)
  }

  function toggleProject(workspaceId: string) {
    setExpandedWorkspaceIds((current) => {
      const next = new Set(current)
      if (next.has(workspaceId)) next.delete(workspaceId)
      else next.add(workspaceId)
      return next
    })
  }

  function showProjectHoverCard(
    event: React.MouseEvent<HTMLButtonElement> | React.FocusEvent<HTMLButtonElement>,
    workspace: Workspace,
    conversationCount: number,
    path: string,
  ) {
    const rect = event.currentTarget.getBoundingClientRect()
    const cardWidth = 280
    const cardHeight = 118
    setProjectHoverCard({
      id: workspace.id,
      name: workspace.name,
      path,
      conversationCount,
      left: Math.min(rect.right + 10, window.innerWidth - cardWidth - 12),
      top: Math.max(12, Math.min(rect.top - 4, window.innerHeight - cardHeight - 12)),
    })
  }

  async function addProjectFromFolder() {
    if (addingProject) return
    setAddingProject(true); setProjectError('')
    try {
      const selection = await api.post<FolderSelection>('/api/system/select-folder', { title: 'Select Project Root' })
      if (!selection.path) return
      const workspace = await api.post<Workspace>('/api/workspaces', { root_path: selection.path })
      await workspaces.refresh()
      setExpandedWorkspaceIds((current) => new Set([...current, workspace.id]))
    } catch (error) {
      setProjectError(describeError(error))
    } finally { setAddingProject(false) }
  }

  async function selectDraftProject() {
    if (pickingDraftProject) return
    setPickingDraftProject(true); setActionError('')
    try {
      const selection = await api.post<FolderSelection>('/api/system/select-folder', { title: 'Select Project Root' })
      if (selection.path) {
        draftVersionRef.current += 1
        setDraftRootPath(selection.path)
      }
    } catch (error) { setActionError(describeError(error)) } finally { setPickingDraftProject(false) }
  }

  function clearDraftProject() {
    draftVersionRef.current += 1
    setDraftRootPath('')
  }

  const {
    closeRunTransport,
    refreshApprovalsForSession,
    startRunStream,
  } = useRunTransport({
    activeIdRef,
    eventSourceRef,
    fallbackTimerRef,
    streamReconnectTimerRef,
    streamErrorCountRef,
    streamRunIdRef,
    seenStreamEventsRef,
    terminalSyncVersionRef,
    liveRunRef,
    setRunsState,
    setApprovalsState,
    setInterruptedRunId,
    setCompletedThoughtsByRun,
    setLiveRun,
    setActionError,
    setChildPanelOpen,
    refreshMessages,
    refreshRuns,
    refreshContext,
    refreshChildTasks,
    refreshTeammates,
    refreshDurableTask,
  })

  useEffect(() => {
    closeRunTransport()
    if (pendingSessionSendRef.current?.sessionId !== activeId) pendingSessionSendRef.current = null
    terminalSyncVersionRef.current += 1
    seenStreamEventsRef.current = { runId: '', eventIds: new Set() }
    setLiveRun(emptyLiveRun())
    stickToBottomRef.current = true
    return () => {
      closeRunTransport()
      terminalSyncVersionRef.current += 1
    }
  }, [activeId, closeRunTransport])

  useEffect(() => {
    const pending = pendingDraftRunRef.current
    if (!pending || pending.sessionId !== activeId) return
    pendingDraftRunRef.current = null
    startRunStream(pending.runId, pending.sessionId)
    void refreshMessages()
    void refreshRuns()
    void refreshContext()
  }, [activeId, refreshContext, refreshMessages, refreshRuns, startRunStream, visibleMessages])

  useEffect(() => {
    if (!activeId || messages.loading || !activeRun?.id || !activeRunCanStream || liveRun.status === 'terminal') return
    if (streamRunIdRef.current === activeRun.id) return
    startRunStream(activeRun.id, activeId)
  }, [activeId, activeRun?.id, activeRun?.status, activeRun?.stop_reason, activeRunCanStream, liveRun.status, messages.loading, startRunStream, visibleMessages])

  // A newly opened history stays pinned until both messages and persisted
  // thought/tool timelines have hydrated. Programmatic layout scroll events
  // must not cancel that initial anchor.
  useEffect(() => {
    const anchoringHistory = Boolean(activeId) && historyScrollSessionRef.current === activeId
    const historyReady = historyHydration.sessionId === activeId
      && historyHydration.complete
      && messages.data.ownerSessionId === activeId
      && runs.data.ownerSessionId === activeId
      && !messages.loading
      && !runs.loading
    if (!shouldStartHistoryScroll(anchoringHistory, historyReady, stickToBottomRef.current)) return
    let prepareFrame = 0
    let frame = 0
    let settleFrame = 0
    let observedElement: HTMLDivElement | null = null
    const finishHistoryAnchor = () => {
      const element = messagesRef.current
      const distanceFromBottom = element
        ? Math.max(0, element.scrollHeight - element.scrollTop - element.clientHeight)
        : Number.POSITIVE_INFINITY
      if (anchoringHistory && historyReady && distanceFromBottom <= 1 && historyScrollSessionRef.current === activeId) {
        historyScrollSessionRef.current = ''
        stickToBottomRef.current = true
      }
    }
    const scrollToLatest = () => {
      const element = messagesRef.current
      if (!element) return
      // Jump once after hydration instead of animating through a long history.
      // This keeps opening and switching conversations immediately responsive.
      observedElement = element
      element.addEventListener('scrollend', finishHistoryAnchor)
      element.scrollTo({ top: element.scrollHeight })
      settleFrame = window.requestAnimationFrame(finishHistoryAnchor)
    }
    // Wait for two browser layout passes after React has committed the fully
    // hydrated history so the final scroll height is stable.
    prepareFrame = window.requestAnimationFrame(() => {
      frame = window.requestAnimationFrame(scrollToLatest)
    })
    return () => {
      window.cancelAnimationFrame(prepareFrame)
      window.cancelAnimationFrame(frame)
      if (settleFrame) window.cancelAnimationFrame(settleFrame)
      observedElement?.removeEventListener('scrollend', finishHistoryAnchor)
    }
  }, [activeId, childPanelOpen, completedThoughtLayoutVersion, historyHydration.complete, historyHydration.sessionId, historyOpenVersion, messages.data.ownerSessionId, messages.loading, liveRun.draft, liveRun.phase, runs.data.ownerSessionId, runs.loading, visibleApprovals.length, visibleMessages.length])

  async function sendMessage(event: FormEvent) {
    event.preventDefault()
    const content = composerInputRef.current?.getValue().trim() || ''
    if (sendingRef.current || (!activeId && !draftActive) || !content || settingsSaving || capabilitySaving || settingsLocked) return
    sendingRef.current = true
    setSending(true); setActionError('')
    lastSubmittedContentRef.current = content
    stickToBottomRef.current = true
    setLiveRun({ ...emptyLiveRun(), phase: '思考中…', status: 'connecting', thought: { ...emptyThoughtTimeline, startedAt: Date.now() }, thinkingStatus: pickThinkingStatus() })
    try {
      if (draftActive) {
        const draftVersion = draftVersionRef.current
        const idempotencyKey = draftIdempotencyKeyRef.current || createDraftIdempotencyKey()
        draftIdempotencyKeyRef.current = idempotencyKey
        const launched = await api.post<DraftLaunchResponse>('/api/drafts/launch', buildDraftLaunchPayload(
          idempotencyKey,
          content,
          draftRootPath,
          draftSettings,
        ))
        if (draftVersionRef.current !== draftVersion) return
        const sessionId = stringId(launched.session?.id)
        const runId = stringId(launched.run?.id)
        if (!sessionId || !runId) throw new Error('草稿启动响应缺少会话或运行标识。')
        setInterruptedRunId('')
        pendingDraftRunRef.current = { sessionId, runId }
        composerInputRef.current?.clear()
        clearDraftState()
        setActiveId(sessionId)
        void sessions.refresh()
        if (draftRootPath || launched.workspace) void workspaces.refresh()
        return
      }

      const targetSessionId = activeId
      const pendingSend = pendingSessionSendRef.current
      const idempotencyKey = pendingSend?.sessionId === targetSessionId && pendingSend.content === content
        ? pendingSend.key
        : createTurnIdempotencyKey()
      pendingSessionSendRef.current = { sessionId: targetSessionId, content, key: idempotencyKey }
      const launched = await api.post<Run>(`/api/sessions/${targetSessionId}/run`, {
        content,
        idempotency_key: idempotencyKey,
      })
      pendingSessionSendRef.current = null
      setInterruptedRunId('')
      composerInputRef.current?.clear()
      void messages.refresh()
      void runs.refresh()
      void context.refresh()
      void durableTask.refresh()
      startRunStream(launched.id, targetSessionId)
    } catch (error) {
      setLiveRun(emptyLiveRun())
      setActionError(describeError(error))
      if (!draftActive && activeId) {
        // The request may have committed before the transport failed. Reload
        // durable state so an accepted turn never remains a bare user bubble.
        void Promise.all([messages.refresh(), runs.refresh(), context.refresh(), durableTask.refresh()])
      }
    } finally {
      sendingRef.current = false
      setSending(false)
    }
  }

  async function stopActiveRun() {
    const runId = interruptibleRunId || streamRunIdRef.current
    if (!runId || stoppingRunId) return
    setStoppingRunId(runId)
    setInterruptedRunId(runId)
    setActionError('')
    setLiveRun((previous) => (
      previous.runId === runId
        ? { ...previous, phase: '正在停止当前任务…', error: '' }
        : previous
    ))
    try {
      await api.post(`/api/runs/${encodeURIComponent(runId)}/stop`, { reason: 'user_interrupted' })
      // The server publishes `run_stopped` on the existing stream. Keep the
      // transport open so the partial assistant draft and terminal timeline
      // can be reconciled by the normal sync path.
      void refreshRuns()
    } catch (error) {
      setInterruptedRunId('')
      if (activeIdRef.current === activeId) setActionError(describeError(error))
      setLiveRun((previous) => (
        previous.runId === runId
          ? { ...previous, phase: previous.phase || '处理中…' }
          : previous
      ))
    } finally {
      setStoppingRunId('')
    }
  }

  async function cancelDurableTask(taskId: string) {
    const sessionId = activeIdRef.current
    if (!sessionId || !taskId || cancellingTaskId) return
    setCancellingTaskId(taskId)
    setActionError('')
    try {
      await api.post(
        `/api/sessions/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(taskId)}/cancel`,
      )
      if (activeIdRef.current !== sessionId) return
      await Promise.all([durableTask.refresh(), runs.refresh()])
    } catch (error) {
      if (activeIdRef.current === sessionId) setActionError(describeError(error))
    } finally {
      setCancellingTaskId('')
    }
  }

  function editInterruptedPrompt() {
    const content = lastSubmittedContentRef.current.trim()
    if (!content || !canEditInterrupted) return
    composerInputRef.current?.setValue(content)
    setInterruptedRunId('')
    setLiveRun(emptyLiveRun())
    setActionError('')
    window.requestAnimationFrame(() => composerInputRef.current?.focus?.())
  }

  async function decideApproval(id: string, decision: 'approve' | 'reject', approvalRunId: string) {
    const approvalSessionId = activeIdRef.current
    if (!approvalSessionId || !approvalRunId || !sessionRuns.some((run) => run.id === approvalRunId)) return
    setActionError(''); setDecidingApproval(id)
    try {
      await api.post(`/api/approvals/${id}/decide`, { decision })
      if (activeIdRef.current !== approvalSessionId) return
      const refreshes: Promise<unknown>[] = [
        refreshApprovalsForSession(approvalSessionId),
        refreshRuns(),
      ]
      if (shouldRefreshConversationAfterApprovalDecision(decision)) refreshes.push(refreshMessages())
      await Promise.all(refreshes)
      if (activeIdRef.current !== approvalSessionId) return
      if (decision === 'reject') {
        setLiveRun((previous) => (
          previous.runId === approvalRunId ? emptyLiveRun() : previous
        ))
        return
      }
      setLiveRun((previous) => {
        if (previous.runId !== approvalRunId || previous.status !== 'awaiting_approval') return previous
        return { ...previous, phase: decision === 'approve' ? '审批已通过，继续处理…' : '正在停止运行…', status: 'connecting', error: '' }
      })
      startRunStream(approvalRunId, approvalSessionId)
    } catch (error) {
      if (activeIdRef.current === approvalSessionId) setActionError(describeError(error))
    } finally { setDecidingApproval('') }
  }

  async function updateSessionSettings(payload: { model_connection_id?: string | null; model_id?: string | null; thinking_level?: ThinkingLevel | null; use_memories?: boolean }) {
    if (settingsLocked || sending) return
    if (draftActive) {
      setDraftSettings((current) => ({
        ...current,
        model_connection_id: payload.model_connection_id === undefined ? current.model_connection_id : payload.model_connection_id,
        model_id: payload.model_id === undefined ? current.model_id : payload.model_id,
        thinking_level: payload.thinking_level ?? current.thinking_level,
        use_memories: payload.use_memories ?? current.use_memories,
      }))
      return
    }
    if (!activeId) return
    setSettingsSaving(true); setActionError('')
    try { await api.patch(`/api/sessions/${activeId}`, payload); await sessions.reload() }
    catch (error) { setActionError(describeError(error)) } finally { setSettingsSaving(false) }
  }

  async function updateSessionCapabilities(payload: { skill_ids?: string[]; mcp_server_names?: string[]; permission_mode?: PermissionMode }) {
    if (settingsLocked || sending || capabilitySaving) return
    if (draftActive) {
      setDraftSettings((current) => ({
        ...current,
        skill_ids: payload.skill_ids ?? current.skill_ids,
        mcp_server_names: payload.mcp_server_names ?? current.mcp_server_names,
        permission_mode: payload.permission_mode ?? current.permission_mode,
      }))
      return
    }
    if (!activeId) return
    setCapabilitySaving(true); setActionError('')
    try { await api.patch(`/api/sessions/${activeId}`, payload); await sessions.reload() }
    catch (error) { setActionError(describeError(error)) } finally { setCapabilitySaving(false) }
  }

  async function deleteConversation(session: Session) {
    const sessionId = stringId(session.id)
    if (!sessionId || deletingSessionId || (sessionId === activeId && sendingRef.current)) return
    const title = session.title || '未命名对话'
    if (!window.confirm(`确定删除“${title}”吗？该对话的消息、运行记录和用量明细将一并删除，且无法恢复。`)) return
    setDeletingSessionId(sessionId)
    setActionError('')
    try {
      await api.delete<void>(`/api/sessions/${encodeURIComponent(sessionId)}`)
      const latest = await sessions.refresh()
      if (sessionId === activeIdRef.current) {
        closeRunTransport()
        terminalSyncVersionRef.current += 1
        setLiveRun(emptyLiveRun())
        setCompletedThoughtsByRun({})
        setActiveId(latest?.[0] ? stringId(latest[0].id) : '')
      }
    } catch (error) {
      setActionError(describeError(error))
    } finally {
      setDeletingSessionId('')
    }
  }

  function renderSessionTreeItem(session: Session, extraClass = '') {
    const sessionId = stringId(session.id)
    const deleting = deletingSessionId === sessionId
    return <div className="tree-session-row" key={session.id}>
      <button type="button" className={`tree-session-item ${extraClass} ${activeId === sessionId && !draftActive ? 'active' : ''}`} disabled={(draftActive && sending) || deleting} onClick={() => openExistingSession(sessionId)}><MessageSquare size={13} /><span>{session.title || '未命名对话'}</span></button>
      <button type="button" className="tree-session-delete" aria-label={`删除对话 ${session.title || '未命名对话'}`} title="删除对话" disabled={deleting || (activeId === sessionId && sending)} onClick={() => void deleteConversation(session)}>{deleting ? <LoaderCircle className="spin" size={12} /> : <Trash2 size={12} />}</button>
    </div>
  }

  const selectedSessionSkillIds = draftActive ? draftSettings.skill_ids : activeSession?.skill_ids ?? []
  const availableMcpServers = mcpServers.data.filter((server) => server.enabled)
  const selectedSessionMcpNames = draftActive ? draftSettings.mcp_server_names : activeSession?.mcp_server_names ?? []
  const selectedPermissionMode: PermissionMode = draftActive ? draftSettings.permission_mode : activeSession?.permission_mode ?? 'smart'
  const selectedUseMemories = draftActive ? draftSettings.use_memories : activeSession?.use_memories ?? true
  const globalMemoriesDisabled = memorySettings.data?.enabled === false

  function toggleSessionSkill(skillId: string) {
    void updateSessionCapabilities({ skill_ids: toggleSelectedId(selectedSessionSkillIds, skillId) })
  }

  function toggleSessionMcp(serverName: string) {
    void updateSessionCapabilities({ mcp_server_names: toggleSelectedId(selectedSessionMcpNames, serverName) })
  }

  function selectPermissionMode(mode: PermissionMode) {
    setPermissionMenuOpen(false)
    void updateSessionCapabilities({ permission_mode: mode })
  }

  const settingsSession: Session | undefined = activeSession ?? (draftActive ? {
    id: 'local-draft',
    model_connection_id: draftSettings.model_connection_id || undefined,
    model_id: draftSettings.model_id || undefined,
    thinking_level: draftSettings.thinking_level,
    use_memories: draftSettings.use_memories,
  } : undefined)
  const effectiveSettings = resolveEffectiveModelSettings(connections.data, settingsSession, activeAgent)
  const effectiveConnection = effectiveSettings.connection
  const effectiveModel = effectiveSettings.model
  const effectiveThinking = resolveEffectiveThinking(settingsSession?.thinking_level, activeAgent?.thinking_level, effectiveConnection?.thinking_level)
  const modelOptions = connections.data.filter((connection) => connection.enabled !== false).flatMap((connection) => {
    return availableConnectionModels(connection).map((model) => ({ value: `${connection.id}::${model}`, label: model, connection: connection.name }))
  })
  const selectedModelValue = effectiveSettings.selectedValue
  const selectedThinkingValue = effectiveThinking
  const modelButtonLabel = shortModelLabel(effectiveModel)
  const thinkingOptions: Array<{ value: ThinkingLevel; label: string; hint?: string }> = [
    { value: 'low', label: '低' },
    { value: 'medium', label: '中' },
    { value: 'high', label: '高' },
    { value: 'xhigh', label: '极高', hint: '更快消耗使用额度' },
  ]
  function closeSettingsMenu() {
    setSettingsMenuOpen(false)
    setSettingsSubmenu(null)
  }
  function selectModel(value: string) {
    closeSettingsMenu()
    void updateSessionSettings(modelSelectionPayload(value))
  }
  function selectThinking(value: ThinkingLevel) {
    closeSettingsMenu()
    void updateSessionSettings({ thinking_level: value })
  }
  function toggleSessionMemories() {
    closeSettingsMenu()
    void updateSessionSettings({ use_memories: !selectedUseMemories })
  }
  function openSettingsSubmenu(kind: 'model' | 'thinking', focusFirst = false) {
    setSettingsSubmenu(kind)
    if (focusFirst) {
      window.requestAnimationFrame(() => {
        const submenu = kind === 'model' ? modelSubmenuRef.current : thinkingSubmenuRef.current
        submenu?.querySelector<HTMLButtonElement>('button')?.focus()
      })
    }
  }
  const dependencyErrors = [agents.error, workspaces.error, connections.error].filter(Boolean)
  return (
    <div className="page page-chat">
      <div className={`chat-shell ${childPanelOpen ? 'child-panel-open' : ''}`}>
          <aside className="session-list project-session-sidebar" onScroll={() => setProjectHoverCard(null)}>
            <section className="draft-tree-section">
              <button type="button" className="new-draft-button" disabled={sending} onClick={beginDraft}><Plus size={14} />新建对话</button>
            </section>
            <section className="project-tree-section">
              <header className="sidebar-section-heading"><strong>项目</strong><button type="button" aria-label="从文件夹添加项目" title="从文件夹添加项目" disabled={addingProject || (draftActive && sending)} onClick={() => void addProjectFromFolder()}>{addingProject ? <LoaderCircle className="spin" size={14} /> : <Plus size={15} />}</button></header>
              {projectError && <div className="sidebar-inline-error"><AlertCircle size={13} /><span>{projectError}</span></div>}
              {sessionNavigation.projects.map(({ workspace, sessions: projectSessions }) => {
                const expanded = expandedWorkspaceIds.has(workspace.id)
                const path = workspace.root_path || workspace.path || '未提供路径'
                return <div className={`project-node ${expanded ? 'expanded' : ''}`} key={workspace.id}>
                  <div className="project-row-wrap" onMouseLeave={() => setProjectHoverCard((current) => current?.id === workspace.id ? null : current)}>
                    <button type="button" className="project-row" aria-expanded={expanded} aria-describedby={projectHoverCard?.id === workspace.id ? 'project-hover-card' : undefined} onMouseEnter={(event) => showProjectHoverCard(event, workspace, projectSessions.length, path)} onFocus={(event) => showProjectHoverCard(event, workspace, projectSessions.length, path)} onBlur={() => setProjectHoverCard((current) => current?.id === workspace.id ? null : current)} onClick={() => toggleProject(workspace.id)}>
                      <ChevronRight className="project-chevron" size={13} /><Folder size={15} /><span>{workspace.name}</span>
                    </button>
                  </div>
                  {expanded && <div className="project-children">
                    {projectSessions.length ? projectSessions.map((session) => renderSessionTreeItem(session)) : <p className="tree-empty">暂无对话</p>}
                  </div>}
                </div>
              })}
              {!sessionNavigation.projects.length && !workspaces.loading && <p className="tree-empty tree-empty-projects">点击右上角 + 添加项目</p>}
            </section>

            <section className="task-tree-section">
              <header className="sidebar-section-heading"><strong>任务</strong></header>
              {sessionNavigation.tasks.map((session) => renderSessionTreeItem(session, 'task-session-item'))}
              {!draftActive && !sessionNavigation.tasks.length && !sessions.loading && <p className="tree-empty">暂无一次性任务</p>}
            </section>

            {sessions.error && <div className="session-list-error"><AlertCircle size={14} /><span>{sessions.error}</span><button type="button" onClick={() => void sessions.reload()}>重试</button></div>}
            {!!dependencyErrors.length && <div className="session-list-error"><AlertCircle size={14} /><span>{dependencyErrors.join('；')}</span><button type="button" onClick={() => { void Promise.all([agents.reload(), workspaces.reload(), connections.reload()]) }}>重试</button></div>}
            {sessions.loading && !sessions.data.length && <LoadingState />}
          </aside>
          <section className={`conversation ${childPanelOpen ? 'with-child-panel' : ''}`}>
            {(activeSession || draftActive) && <button type="button" className="child-panel-toggle conversation-side-toggle" aria-label={childPanelOpen ? '收起子 Agent 面板' : '打开子 Agent 面板'} title={childPanelOpen ? '收起子 Agent 面板' : '打开子 Agent 面板'} aria-expanded={childPanelOpen} onClick={() => setChildPanelOpen((open) => !open)}>{childPanelOpen ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}</button>}
            {activeSession || draftActive ? <>
              <div
                className="messages"
                ref={messagesRef}
                aria-live="polite"
                onScroll={(event) => {
                  const element = event.currentTarget
                  const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight
                  if (activeId && historyScrollSessionRef.current === activeId) {
                    const historyReady = historyHydration.sessionId === activeId
                      && historyHydration.complete
                      && messages.data.ownerSessionId === activeId
                      && runs.data.ownerSessionId === activeId
                      && !messages.loading
                      && !runs.loading
                    if (historyReady && distanceFromBottom === 0) {
                      historyScrollSessionRef.current = ''
                      stickToBottomRef.current = true
                    }
                    return
                  }
                  stickToBottomRef.current = distanceFromBottom < 80
                }}
              >
                {draftActive ? liveRun.status === 'idle' && <EmptyState icon={MessageSquare} title="开始一次新任务" description="直接描述目标；需要处理本地文件时，可以在输入框中选择一个项目文件夹。" /> : messages.error && !visibleMessages.length ? <ErrorState message={messages.error} onRetry={messages.reload} /> : messages.loading && !visibleMessages.length ? <LoadingState /> : visibleMessages.length ? visibleMessages.map((message) => {
                  const messageRunId = message.role === 'assistant' ? stringId(message.metadata?.run_id) : ''
                  const completedThought = messageRunId ? completedThoughtsByRun[messageRunId] : undefined
                  return <Fragment key={message.id}>{completedThought && <CompletedThoughtTimeline runId={messageRunId} timeline={completedThought} />}<MessageBubble message={message} /></Fragment>
                }) : liveRun.status === 'idle' ? <EmptyState icon={MessageSquare} title="从一条清晰的任务开始" description="描述目标、约束和期望产物，Agent 会先理解上下文再行动。" /> : null}
                {!draftActive && messages.error && !!visibleMessages.length && <p className="inline-error" role="alert">消息同步失败：{messages.error}</p>}
                {!draftActive && durableTask.data && <DurableTaskCard
                  task={durableTask.data}
                  cancelling={cancellingTaskId === durableTask.data.id}
                  onResume={() => {
                    composerInputRef.current?.setValue('继续刚刚的工作')
                    composerInputRef.current?.focus()
                  }}
                  onCancel={() => void cancelDurableTask(durableTask.data!.id)}
                />}
                {!draftActive && stoppedRunNotices.map((run) => <div key={`run-notice:${run.id}`} className="stopped-run-notice" role="status"><AlertCircle size={16} /><div><strong>{run.status === 'failed' ? '本次运行失败，未生成最终回复' : '本次运行已停止，未生成最终回复'}</strong><p>{run.error_message || run.stop_reason || 'Agent 未能继续执行，请调整指令后重试。'}</p></div></div>)}
                {liveRun.status !== 'idle'
                  && (!completedThoughtsByRun[liveRun.runId] || (canEditInterrupted && liveRun.runId === interruptedRunId))
                  && <LiveAssistantMessage liveRun={liveRun} />}
                {canEditInterrupted && <div className="interrupted-run-actions">
                  <button type="button" className="interrupted-edit-button" onClick={editInterruptedPrompt}>
                    <Pencil size={12} />重新编辑本次输入
                  </button>
                  <span>已保留当前部分输出，重新发送前不会写入新的上下文。</span>
                </div>}
                {!draftActive && approvals.error && <ErrorState message={`审批状态读取失败：${approvals.error}`} onRetry={approvals.reload} />}
                {!draftActive && approvals.loading && activeRunId && !visibleApprovals.length && liveRun.status === 'awaiting_approval' && <LoadingState label="正在读取审批状态" />}
                {!draftActive && visibleApprovals.map((approval) => <ApprovalCard key={approval.id} approval={approval} deciding={decidingApproval === approval.id} onDecision={decideApproval} />)}
              </div>
              <form className="composer" onSubmit={sendMessage}>
                {actionError && <p className="form-error" role="alert">{actionError}</p>}
                {draftActive && <div className="draft-project-controls">
                  <button type="button" className="draft-project-button" disabled={pickingDraftProject || sending} onClick={() => void selectDraftProject()}>{pickingDraftProject ? <LoaderCircle className="spin" size={13} /> : <FolderOpen size={13} />}选择项目</button>
                  {draftRootPath && <span className="draft-folder-pill" title={draftRootPath}><Folder size={12} /><span>{folderName(draftRootPath)}</span><button type="button" aria-label="清除所选项目" disabled={sending} onClick={clearDraftProject}><X size={11} /></button></span>}
                </div>}
                <ComposerTextArea ref={composerInputRef} disabled={draftActive && sending} resetKey={`${activeId}:${draftActive}`} placeholder={draftActive ? '描述你想完成的任务……' : '告诉 PGAgent 你想完成什么……'} onHasValueChange={setComposerHasValue} />
                <div className="composer-toolbar">
                  <div className="composer-left-actions">
                    <div className="session-capability-picker" ref={addMenuRef}>
                      <button type="button" className="composer-tool-button composer-plus-button" aria-label="添加能力" aria-haspopup="menu" aria-expanded={addMenuOpen} aria-busy={capabilitySaving} disabled={settingsLocked || sending || capabilitySaving} onClick={() => { setAddMenuOpen((open) => !open); setSkillSubmenuOpen(false); setMcpSubmenuOpen(false); setPermissionMenuOpen(false) }}>
                        <Plus size={15} />
                        {!!(selectedSessionSkillIds.length + selectedSessionMcpNames.length) && <b>{selectedSessionSkillIds.length + selectedSessionMcpNames.length}</b>}
                      </button>
                      {addMenuOpen && <div className="capability-popover capability-level-two" role="menu" aria-label="添加能力">
                        <button type="button" className={skillSubmenuOpen ? 'active' : ''} role="menuitem" aria-haspopup="menu" aria-expanded={skillSubmenuOpen} onMouseEnter={() => { setSkillSubmenuOpen(true); setMcpSubmenuOpen(false) }} onClick={() => { setSkillSubmenuOpen((open) => !open); setMcpSubmenuOpen(false) }}><BookOpen size={14} /><span>Skill</span><small>{selectedSessionSkillIds.length ? `已选 ${selectedSessionSkillIds.length}` : '未选择'}</small><ChevronRight size={13} /></button>
                        {skillSubmenuOpen && <div className="capability-popover capability-level-three" role="menu" aria-label="选择 Skill">
                          <p>可用 Skill</p>
                          {skills.error ? <div className="capability-menu-state error"><AlertCircle size={13} /><span>{skills.error}</span><button type="button" onClick={() => void skills.reload()}>重试</button></div>
                            : skills.loading ? <div className="capability-menu-state"><LoaderCircle className="spin" size={13} />正在读取…</div>
                              : skills.data.length ? skills.data.map((skill) => {
                                const selected = selectedSessionSkillIds.includes(skill.id)
                                return <button key={skill.id} type="button" role="menuitemcheckbox" aria-checked={selected} className={selected ? 'selected' : ''} disabled={skill.enabled === false || capabilitySaving} onClick={() => toggleSessionSkill(skill.id)}><span><strong>{skill.name}</strong><small>{skill.description || skill.slug || 'Skill'}</small></span><Check className="selection-check" size={14} aria-hidden="true" /></button>
                              }) : <div className="capability-menu-state">技能库中暂无 Skill。</div>}
                        </div>}
                        <button type="button" className={mcpSubmenuOpen ? 'active' : ''} role="menuitem" aria-haspopup="menu" aria-expanded={mcpSubmenuOpen} onMouseEnter={() => { setMcpSubmenuOpen(true); setSkillSubmenuOpen(false) }} onClick={() => { setMcpSubmenuOpen((open) => !open); setSkillSubmenuOpen(false) }}><Cable size={14} /><span>MCP</span><small>{selectedSessionMcpNames.length ? `已选 ${selectedSessionMcpNames.length}` : '不使用'}</small><ChevronRight size={13} /></button>
                        {mcpSubmenuOpen && <div className="capability-popover capability-level-three" role="menu" aria-label="选择 MCP">
                          <p>当前会话使用的 MCP</p>
                          <button type="button" role="menuitem" className={!selectedSessionMcpNames.length ? 'selected' : ''} disabled={capabilitySaving} onClick={() => void updateSessionCapabilities({ mcp_server_names: [] })}><span><strong>不使用 MCP</strong><small>本会话不连接任何 MCP 服务器</small></span><Check className="selection-check" size={14} aria-hidden="true" /></button>
                          {mcpServers.error ? <div className="capability-menu-state error"><AlertCircle size={13} /><span>{mcpServers.error}</span><button type="button" onClick={() => void mcpServers.reload()}>重试</button></div>
                            : mcpServers.loading ? <div className="capability-menu-state"><LoaderCircle className="spin" size={13} />正在读取…</div>
                              : availableMcpServers.length ? availableMcpServers.map((server) => {
                                const selected = selectedSessionMcpNames.includes(server.name)
                                return <button key={server.name} type="button" role="menuitemcheckbox" aria-checked={selected} className={selected ? 'selected' : ''} disabled={capabilitySaving} onClick={() => toggleSessionMcp(server.name)}><span><strong>{server.name}</strong><small>{server.transport === 'stdio' ? [server.command, ...server.args].filter(Boolean).join(' ') : 'Streamable HTTP'}</small></span><Check className="selection-check" size={14} aria-hidden="true" /></button>
                              }) : <div className="capability-menu-state">暂无已启用的 MCP 服务器</div>}
                        </div>}
                      </div>}
                    </div>
                    <div className="session-capability-picker permission-picker" ref={permissionMenuRef}>
                      <button type="button" className="composer-tool-button permission-trigger" aria-label={`权限模式：${permissionLabel(selectedPermissionMode)}`} aria-haspopup="menu" aria-expanded={permissionMenuOpen} disabled={settingsLocked || sending || capabilitySaving} onClick={() => { setPermissionMenuOpen((open) => !open); setAddMenuOpen(false); setSkillSubmenuOpen(false); setMcpSubmenuOpen(false) }}><ShieldCheck size={14} /><span>{permissionLabel(selectedPermissionMode)}</span><ChevronRight size={12} /></button>
                      {permissionMenuOpen && <div className="capability-popover permission-popover" role="menu" aria-label="权限模式">
                        <p>权限</p>
                        {permissionOptions.map((option) => <button key={option.value} type="button" role="menuitemradio" aria-checked={selectedPermissionMode === option.value} className={selectedPermissionMode === option.value ? 'selected' : ''} onClick={() => selectPermissionMode(option.value)}><span>{option.label}</span><Check className="selection-check" size={14} aria-hidden="true" /></button>)}
                      </div>}
                    </div>
                  </div>
                  <div className="composer-actions">
                    <div className="session-settings-picker" ref={settingsMenuRef} title={settingsLocked ? '当前运行结束或审批完成后才能切换模型和思考强度' : undefined}>
                      <button
                        ref={settingsTriggerRef}
                        type="button"
                        className="session-settings-trigger"
                        aria-label={`当前模型 ${modelButtonLabel}，思考强度 ${thinkingLevelLabels[effectiveThinking]}。点击更改`}
                        aria-haspopup="menu"
                        aria-expanded={settingsMenuOpen}
                        aria-busy={settingsSaving}
                        disabled={settingsSaving || settingsLocked || sending}
                        onClick={() => { setSettingsMenuOpen((open) => !open); setSettingsSubmenu(null) }}
                      >
                        <span>{modelButtonLabel}</span><strong>{thinkingLevelLabels[effectiveThinking]}</strong><ChevronRight size={12} aria-hidden="true" />
                      </button>
                      {settingsMenuOpen && <div className="session-settings-popover" role="menu" aria-label="模型和思考设置">
                        <button type="button" className="session-memory-setting" role="menuitemcheckbox" aria-checked={selectedUseMemories} aria-busy={settingsSaving} disabled={globalMemoriesDisabled || settingsSaving || settingsLocked || sending} onClick={toggleSessionMemories}>
                          <span><strong>使用已有记忆</strong><small>{globalMemoriesDisabled ? '全局已关闭' : selectedUseMemories ? '此会话会使用已有记忆' : '此会话不会使用已有记忆'}</small></span>
                          <span className="session-memory-switch" aria-hidden="true"><span /></span>
                        </button>
                        <button type="button" className={settingsSubmenu === 'model' ? 'active' : ''} role="menuitem" aria-haspopup="menu" aria-expanded={settingsSubmenu === 'model'} onMouseEnter={() => openSettingsSubmenu('model')} onKeyDown={(event) => { if (event.key === 'ArrowRight') { event.preventDefault(); openSettingsSubmenu('model', true) } }} onClick={() => openSettingsSubmenu('model', true)}>
                          <span>模型</span><small>{modelButtonLabel}</small><ChevronRight size={13} aria-hidden="true" />
                        </button>
                        {settingsSubmenu === 'model' && <div className="session-settings-submenu" ref={modelSubmenuRef} role="menu" aria-label="选择模型">
                          <p>模型</p>
                          {modelOptions.map((option, index) => <button key={`${option.value}:${index}`} type="button" role="menuitemradio" aria-checked={selectedModelValue === option.value} className={selectedModelValue === option.value ? 'selected' : ''} onClick={() => selectModel(option.value)}>
                            <span><strong>{option.label}</strong><small>{option.connection}</small></span><Check className="selection-check" size={14} aria-hidden="true" />
                          </button>)}
                        </div>}
                        <button type="button" className={settingsSubmenu === 'thinking' ? 'active' : ''} role="menuitem" aria-haspopup="menu" aria-expanded={settingsSubmenu === 'thinking'} onMouseEnter={() => openSettingsSubmenu('thinking')} onKeyDown={(event) => { if (event.key === 'ArrowRight') { event.preventDefault(); openSettingsSubmenu('thinking', true) } }} onClick={() => openSettingsSubmenu('thinking', true)}>
                          <span>推理强度</span><small>{thinkingLevelLabels[effectiveThinking]}</small><ChevronRight size={13} aria-hidden="true" />
                        </button>
                        {settingsSubmenu === 'thinking' && <div className="session-settings-submenu" ref={thinkingSubmenuRef} role="menu" aria-label="选择推理强度">
                          <p>推理强度</p>
                          {thinkingOptions.map((option) => <button key={option.value} type="button" role="menuitemradio" aria-checked={selectedThinkingValue === option.value} className={selectedThinkingValue === option.value ? 'selected' : ''} onClick={() => selectThinking(option.value)}>
                            <span><strong>{option.label}</strong>{option.hint && <small>{option.hint}</small>}</span><Check className="selection-check" size={14} aria-hidden="true" />
                          </button>)}
                        </div>}
                      </div>}
                    </div>
                    {draftActive ? <ContextUsageRing context={emptyDraftContext} /> : context.error ? <button type="button" className="context-state context-error" aria-label="上下文占用读取失败，点击重试" title={context.error} onClick={() => void context.reload()}><AlertCircle size={15} /></button> : context.loading || !context.data ? <span className="context-state" role="status" aria-label="正在读取上下文占用"><LoaderCircle className="spin" size={15} /></span> : <ContextUsageRing context={context.data} />}
                    <button
                      type={canInterrupt ? 'button' : 'submit'}
                      className={`send-button ${canInterrupt ? 'is-stop' : ''}`}
                      aria-label={canInterrupt ? '中断当前任务' : '发送'}
                      title={canInterrupt ? '中断当前任务' : '发送'}
                      aria-busy={Boolean(stoppingRunId)}
                      disabled={canInterrupt
                        ? Boolean(stoppingRunId)
                        : sending || settingsSaving || capabilitySaving || settingsLocked || !composerHasValue}
                      onClick={canInterrupt ? () => void stopActiveRun() : undefined}
                    >
                      {canInterrupt
                        ? stoppingRunId ? <LoaderCircle className="spin" size={14} /> : <Square className="send-stop-icon" size={12} strokeWidth={3} fill="currentColor" />
                        : <ArrowUp className="send-arrow-icon" size={16} strokeWidth={2.4} />}
                    </button>
                  </div>
                </div>
              </form>
            </> : <EmptyState icon={MessageSquare} title="开始新对话" description="创建一个临时草稿；首次发送后才会保存为任务或项目对话。" action={<button className="button button-primary" onClick={beginDraft}>新建对话</button>} />}
          </section>
          <ChildAgentPanel
            open={childPanelOpen}
            tasks={visibleChildTasks}
            teammates={visibleTeammates}
            loading={childTasks.loading}
            error={childTasks.error}
            selectedTask={activeChildTask}
            run={childTaskRun}
            events={childTaskEvents.data}
            eventsLoading={childTaskEvents.loading}
            eventsError={childTaskEvents.error}
            onClose={() => setChildPanelOpen(false)}
            onSelect={setSelectedChildTaskId}
            onRetry={() => { void childTasks.reload(); void teammates.reload(); void childTaskEvents.reload() }}
          />
      </div>
      {projectHoverCard && createPortal(
        <div className="project-hover-card" id="project-hover-card" role="tooltip" style={{ left: projectHoverCard.left, top: projectHoverCard.top }}>
          <div className="project-hover-card-row project-hover-card-title"><Folder size={14} /><strong>{projectHoverCard.name}</strong></div>
          <div className="project-hover-card-row"><MessageSquare size={14} /><span>{projectHoverCard.conversationCount} 个对话</span></div>
          <div className="project-hover-card-row project-hover-card-path"><Folder size={14} /><span>{projectHoverCard.path}</span></div>
        </div>,
        document.body,
      )}
    </div>
  )
}


export { SessionsPage }
