// 本文件实现 SessionsPage 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
import { AlertCircle, ArrowUp, BookOpen, Cable, Check, ChevronRight, FileText, Folder, FolderOpen, LoaderCircle, MessageSquare, PanelRightClose, PanelRightOpen, Paperclip, Pencil, Plus, ShieldAlert, ShieldCheck, Square, Trash2, X } from 'lucide-react'
import { type CSSProperties, type FormEvent, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type SetStateAction, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, describeError } from '../../api'
import { attachmentForm, attachmentSignature, formatAttachmentSize, selectAttachmentFiles } from '../../attachments'
import type { PendingAttachment } from '../../attachments'
import { permissionLabel, permissionOptions, toggleSelectedId } from '../../capabilitySelection'
import { modelSelectionPayload, resolveEffectiveThinking, sessionThinkingOptions, shortModelLabel, thinkingLevelLabels } from '../../composerSettings'
import { buildDraftLaunchPayload, createDraftIdempotencyKey, createTurnIdempotencyKey } from '../../draftLaunch'
import { availableConnectionModels, resolveEffectiveModelSettings } from '../../modelSettings'
import { buildSessionNavigation, draftSessionTitle, folderName, isDefaultWorkspace, mergePendingSession, pendingSessionAfterRemoval, projectRootForSession } from '../../sessionNavigation'
import { composerSurface, isResumableWaitingRun, isTerminalRunStatus, shouldRefreshConversationAfterApprovalDecision, shouldShowStoppedRunNotice, shouldStartHistoryScroll, visibleSessionItems } from '../../sessionStream'
import { emptyThoughtTimeline, hasVisibleCompletedThought, pickThinkingStatus, timelineFromRunEvents } from '../../thoughtTimeline'
import type { ThoughtTimelineState } from '../../thoughtTimeline'
import { ThoughtHydrationRegistry } from '../../thoughtHydration'
import type { ThoughtHydrationToken } from '../../thoughtHydration'
import { ContextUsageRing, EmptyState, ErrorState, LoadingState } from '../../components/ui'
import { ApprovalCard, ChildAgentPanel, LiveAssistantMessage, MessageBubble } from './presentation'
import { useApiData } from '../../shared/hooks/useApiData'
import { stringId } from '../../shared/lib/display'
import { ComposerTextArea } from './components/ComposerTextArea'
import type { ComposerTextAreaHandle } from './components/ComposerTextArea'
import { ConversationTurnTimeline } from './components/ConversationTurnTimeline'
import { FileChangePanel } from './components/FileChangePanel'
import { ProjectTreeItem } from './components/ProjectTreeItem'
import { projectDeleteConfirmation } from './projectDeletion'
import { useRunTransport } from './hooks/useRunTransport'
import { activeRunStatuses, draftSettingsWithPermission, emptyDraftContext, emptyDraftSettings, emptyLiveRun, noDelegatedTasks, noTeammates, removePendingApproval, runThinkingStartedAt } from './sessionState'
import type { DraftLaunchResponse, DraftSessionSettings, LiveRunState, OwnedSessionDelegations, OwnedSessionMessages, OwnedSessionRuns, ProjectHoverCard } from './sessionState'
import { buildConversationTurnSummaries } from './turnTimeline'
import type { AgentProfile, Approval, Connection, DelegatedTask, DurableTask, FileChangeSelection, FolderSelection, McpServer, MemorySettings, Message, PermissionMode, PermissionSettings, Run, RunEvent, Session, SessionContext, SkillCatalogItem, Teammate, ThinkingLevel, Workspace } from '../../types'

type ChildPanelState = { sessionId: string; open: boolean; autoOpened: boolean }
type WorkspaceExpansion = { contextKey: string; ids: Set<string> }
type SidePanelResizeBounds = { min: number; max: number }
type SidePanelResizeInteraction = SidePanelResizeBounds & { pointerId: number; startX: number; startWidth: number }

const SIDE_PANEL_DEFAULT_WIDTH = 290
const SIDE_PANEL_MIN_WIDTH = 240
const SIDE_PANEL_MAX_WIDTH = 620
const CONVERSATION_MIN_WIDTH = 320

// 分隔条的计算保持为纯函数，鼠标和键盘调整共用同一套边界规则。
function clampSidePanelWidth(width: number, bounds: SidePanelResizeBounds): number {
  return Math.min(bounds.max, Math.max(bounds.min, width))
}

function sidePanelWidthAfterDrag(startWidth: number, startX: number, currentX: number, bounds: SidePanelResizeBounds): number {
  return clampSidePanelWidth(startWidth + startX - currentX, bounds)
}

// 首次加载会话列表时直接派生选中项，草稿模式则始终保持未持久化状态。
function resolveActiveSessionId(selectedSessionId: string, draftActive: boolean, sessions: Session[]): string {
  return draftActive ? '' : selectedSessionId || stringId(sessions[0]?.id)
}

function nextSelectedSessionId(current: string, sessions: Session[]): string {
  return current || stringId(sessions[0]?.id)
}

// 子任务首次出现时自动打开；手动关闭持续到任务清空或切换会话。
function nextChildPanelStateForTasks(current: ChildPanelState, sessionId: string, tasks: DelegatedTask[]): ChildPanelState {
  const scoped = current.sessionId === sessionId
    ? current
    : { sessionId, open: current.open, autoOpened: false }
  if (!tasks.length) return { ...scoped, autoOpened: false }
  return scoped.autoOpened ? scoped : { ...scoped, open: true, autoOpened: true }
}

// 展开状态只属于创建它的会话或草稿；切换上下文时改用当前项目作为默认值。
function resolveExpandedWorkspaceIds(
  expansion: WorkspaceExpansion | null,
  contextKey: string,
  defaultWorkspaceId: string,
): Set<string> {
  if (expansion?.contextKey === contextKey) return expansion.ids
  return defaultWorkspaceId ? new Set([defaultWorkspaceId]) : new Set()
}

// 菜单的打开请求必须仍属于当前会话/运行上下文，运行锁定后不再重新显现。
function resolveMenuOpen(open: boolean, openedContextKey: string, currentContextKey: string, locked: boolean): boolean {
  return open && !locked && openedContextKey === currentContextKey
}

// SessionsPage 是会话工作台协调器：连接项目/会话导航、消息历史、实时运行、审批、子任务和编辑器设置。
function SessionsPage() {
  // 首组资源是页面级目录数据，提供会话归属、Agent 默认值以及可选择的模型与能力。
  const [selectedSessionId, setActiveId] = useState('')
  const sessions = useApiData<Session[]>([], async () => {
    const items = await api.list<Session>('/api/sessions', ['sessions'])
    setActiveId((current) => nextSelectedSessionId(current, items))
    return items
  }, [])
  const agents = useApiData<AgentProfile[]>([], () => api.list<AgentProfile>('/api/agents', ['agents']), [])
  const workspaces = useApiData<Workspace[]>([], () => api.list<Workspace>('/api/workspaces', ['workspaces']), [])
  const connections = useApiData<Connection[]>([], () => api.list<Connection>('/api/connections', ['connections']), [])
  const skills = useApiData<SkillCatalogItem[]>([], () => api.list<SkillCatalogItem>('/api/skills', ['skills']), [])
  const mcpServers = useApiData<McpServer[]>([], () => api.list<McpServer>('/api/mcp/servers', ['mcp-servers']), [])
  const memorySettings = useApiData<MemorySettings | null>(null, () => api.get<MemorySettings>('/api/memories/settings'), [])
  const permissionSettings = useApiData<PermissionSettings | null>(null, () => api.get<PermissionSettings>('/api/permissions/settings'), [])
  // activeId 选择当前会话；编辑器、发送、中断和删除状态共同描述当前用户操作。
  const [composerHasValue, setComposerHasValue] = useState(false)
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([])
  const [sending, setSending] = useState(false)
  const [stoppingRunId, setStoppingRunId] = useState('')
  const [cancellingTaskId, setCancellingTaskId] = useState('')
  const [deletingSessionId, setDeletingSessionId] = useState('')
  const [deletingWorkspaceId, setDeletingWorkspaceId] = useState('')
  const [interruptedRunId, setInterruptedRunId] = useState('')
  const [lastSubmittedContent, setLastSubmittedContent] = useState('')
  // 下列状态控制设置菜单及其子菜单；capabilitySaving 单独标记技能/MCP/权限写入。
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [settingsMenuOpen, setSettingsMenuOpen] = useState(false)
  const [settingsSubmenu, setSettingsSubmenu] = useState<'model' | 'thinking' | null>(null)
  const [addMenuOpen, setAddMenuOpen] = useState(false)
  const [skillSubmenuOpen, setSkillSubmenuOpen] = useState(false)
  const [mcpSubmenuOpen, setMcpSubmenuOpen] = useState(false)
  const [permissionMenuOpen, setPermissionMenuOpen] = useState(false)
  const [openedMenuContextKey, setOpenedMenuContextKey] = useState('')
  const [capabilitySaving, setCapabilitySaving] = useState(false)
  // 菜单 DOM 引用用于点击外部关闭和键盘焦点管理。
  const settingsMenuRef = useRef<HTMLDivElement>(null)
  const settingsTriggerRef = useRef<HTMLButtonElement>(null)
  const modelSubmenuRef = useRef<HTMLDivElement>(null)
  const thinkingSubmenuRef = useRef<HTMLDivElement>(null)
  const addMenuRef = useRef<HTMLDivElement>(null)
  const permissionMenuRef = useRef<HTMLDivElement>(null)
  const chatShellRef = useRef<HTMLDivElement>(null)
  const sidePanelResizeRef = useRef<SidePanelResizeInteraction | null>(null)
  // actionError 汇集会话动作失败；draft* 表示尚未持久化的新会话及其项目/设置。
  const [decidingApproval, setDecidingApproval] = useState('')
  const [actionError, setActionError] = useState('')
  const [draftActive, setDraftActive] = useState(false)
  const [pendingSession, setPendingSession] = useState<Session | null>(null)
  const activeId = resolveActiveSessionId(selectedSessionId, draftActive, sessions.data)
  const [draftRootPath, setDraftRootPath] = useState('')
  const [draftSettings, setDraftSettings] = useState<DraftSessionSettings>(emptyDraftSettings)
  // 项目树展开、文件夹选择、悬浮卡片，以及完成思考/子 Agent 面板属于展示层状态。
  const [workspaceExpansion, setWorkspaceExpansion] = useState<WorkspaceExpansion | null>(null)
  const [addingProject, setAddingProject] = useState(false)
  const [pickingDraftProject, setPickingDraftProject] = useState(false)
  const [projectError, setProjectError] = useState('')
  const [projectHoverCard, setProjectHoverCard] = useState<ProjectHoverCard | null>(null)
  const [completedThoughtsByRun, setCompletedThoughtsByRun] = useState<Record<string, ThoughtTimelineState>>({})
  const [childPanelState, setChildPanelState] = useState<ChildPanelState>({ sessionId: '', open: false, autoOpened: false })
  const [selectedChildTaskId, setSelectedChildTaskId] = useState('')
  const [durableSourceOpen, setDurableSourceOpen] = useState(false)
  const [selectedDurableTaskId, setSelectedDurableTaskId] = useState('')
  const [fileChangeSelection, setFileChangeSelection] = useState<FileChangeSelection | null>(null)
  const [sidePanelWidth, setSidePanelWidth] = useState<number | null>(null)
  const [sidePanelResizing, setSidePanelResizing] = useState(false)
  const [sidePanelResizeBounds, setSidePanelResizeBounds] = useState<SidePanelResizeBounds>({ min: SIDE_PANEL_MIN_WIDTH, max: SIDE_PANEL_MAX_WIDTH })
  // 运输层 refs 跨渲染保存 EventSource、计时器、事件去重集合和当前运行 ID，交给 useRunTransport 管理。
  const [liveRun, setLiveRun] = useState<LiveRunState>(emptyLiveRun)
  const eventSourceRef = useRef<EventSource | null>(null)
  const fallbackTimerRef = useRef<number | null>(null)
  const streamReconnectTimerRef = useRef<number | null>(null)
  const streamErrorCountRef = useRef(0)
  const streamRunIdRef = useRef('')
  const seenStreamEventsRef = useRef<{ runId: string; eventIds: Set<string> }>({ runId: '', eventIds: new Set() })
  // DOM/提交 refs 保存滚动位置、输入组件、待上传文件与幂等键，避免异步回调捕获旧状态。
  const messagesRef = useRef<HTMLDivElement>(null)
  const composerInputRef = useRef<ComposerTextAreaHandle>(null)
  const attachmentInputRef = useRef<HTMLInputElement>(null)
  const pendingAttachmentsRef = useRef<PendingAttachment[]>([])
  const activeIdRef = useRef('')
  const stickToBottomRef = useRef(true)
  const historyScrollSessionRef = useRef('')
  const terminalSyncVersionRef = useRef(0)
  const pendingDraftRunRef = useRef<{ sessionId: string; runId: string; startedAt?: string } | null>(null)
  const draftIdempotencyKeyRef = useRef('')
  const pendingSessionSendRef = useRef<{ sessionId: string; content: string; attachmentSignature: string; key: string } | null>(null)
  const draftVersionRef = useRef(0)
  const sendingRef = useRef(false)
  const thoughtHydrationRegistryRef = useRef(new ThoughtHydrationRegistry())
  // 历史水合状态区分“数据已到达”和“滚动锚定已完成”，防止首次打开跳动。
  const [historyHydration, setHistoryHydration] = useState({ sessionId: '', complete: false })
  const [historyOpenVersion, setHistoryOpenVersion] = useState(0)
  const liveRunRef = useRef(liveRun)

  useLayoutEffect(() => {
    activeIdRef.current = activeId
    liveRunRef.current = liveRun
    pendingAttachmentsRef.current = pendingAttachments
  }, [activeId, liveRun, pendingAttachments])

  useEffect(() => {
    setFileChangeSelection(null)
  }, [activeId])

  useEffect(() => () => {
    pendingAttachmentsRef.current.forEach((item) => {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl)
    })
  }, [])

  // 会话关联资源均携带 ownerSessionId；异步结果返回后只有 owner 与 activeId 一致才会展示。
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
    const sessionId = activeId
    const items = await api.list<DelegatedTask>(`/api/sessions/${encodeURIComponent(sessionId)}/delegations`, ['delegations'])
    if (activeIdRef.current === sessionId) {
      setChildPanelState((current) => nextChildPanelStateForTasks(current, sessionId, items))
      setSelectedChildTaskId((current) => items.some((task) => task.id === current) ? current : stringId(items[0]?.id))
    }
    return {
      ownerSessionId: sessionId,
      items,
    }
  }, [activeId])
  const teammates = useApiData<Teammate[]>([], () => activeId
    ? api.list<Teammate>(`/api/sessions/${encodeURIComponent(activeId)}/teammates`, ['teammates'])
    : Promise.resolve([]), [activeId])
  const durableTask = useApiData<DurableTask | null>(null, () => activeId
    ? api.get<DurableTask | null>(`/api/sessions/${encodeURIComponent(activeId)}/active-task`)
    : Promise.resolve(null), [activeId])
  const durableTasks = useApiData<DurableTask[]>([], () => activeId
    ? api.list<DurableTask>(`/api/sessions/${encodeURIComponent(activeId)}/tasks`, ['durable-tasks'])
    : Promise.resolve([]), [activeId])
  const refreshDurableTask = durableTask.refresh
  const refreshDurableTasks = durableTasks.refresh
  const refreshDurableTaskState = useCallback(async () => {
    await Promise.all([refreshDurableTask(), refreshDurableTasks()])
  }, [refreshDurableTask, refreshDurableTasks])
  const context = useApiData<SessionContext | null>(null, () => activeId ? api.get<SessionContext>(`/api/sessions/${activeId}/context`) : Promise.resolve(null), [activeId])
  // 以下派生值把原始资源收敛为当前会话、当前运行、可见消息及可操作状态。
  const activeSession = sessions.data.find((item) => stringId(item.id) === activeId)
    ?? (pendingSession && stringId(pendingSession.id) === activeId ? pendingSession : undefined)
  const activeAgent = agents.data.find((item) => item.id === activeSession?.agent_id)
  const workspaceExpansionContextKey = draftActive ? 'draft' : `session:${activeId}`
  const activeWorkspaceId = activeSession?.workspace_id || ''
  const defaultExpandedWorkspaceId = !draftActive && workspaces.data.some((workspace) => workspace.id === activeWorkspaceId && !isDefaultWorkspace(workspace))
    ? activeWorkspaceId
    : ''
  const expandedWorkspaceIds = resolveExpandedWorkspaceIds(workspaceExpansion, workspaceExpansionContextKey, defaultExpandedWorkspaceId)
  const sessionRuns = visibleSessionItems(runs.data.ownerSessionId, activeId, runs.data.items)
    .filter((item) => !item.session_id || item.session_id === activeId)
  // awaitingApprovalRunIds 驱动审批查询；只关注当前会话中仍等待决策的运行。
  const awaitingApprovalRunIds = sessionRuns
    .filter((item) => item.status === 'awaiting_approval')
    .map((item) => item.id)
  const approvalRunIdsKey = awaitingApprovalRunIds.join(',')
  // 等待审批的子运行优先于父运行，确保审批卡直接出现在当前会话，而不被已为子任务停止的父运行遮挡。
  const activeRun = sessionRuns.find((item) => item.status === 'awaiting_approval')
    ?? sessionRuns.find((item) => isResumableWaitingRun(item))
    ?? sessionRuns.find((item) => activeRunStatuses.has(item.status || ''))
    ?? sessionRuns[0]
  const activeRunId = activeRun?.id || ''
  const activeRunCanStream = Boolean(activeRun && (
    activeRunStatuses.has(activeRun.status || '') || isResumableWaitingRun(activeRun)
  ))
  // `sending` only covers the initial launch request. Once the request has
  // 子任务返回后，只要 SSE 或兜底轮询仍工作，运行就仍活跃；编辑器继续绑定该运行以允许随时中断。
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
      && lastSubmittedContent.trim(),
  )
  // 旧版本曾把子任务原始响应写入聊天记录；这些行也隐藏，当前权威展示位于子 Agent 侧栏。
  // visibleMessages 与 repliedRunIds 用于渲染历史，并判定终止运行是否已有权威回复。
  const visibleMessages = visibleSessionItems(messages.data.ownerSessionId, activeId, messages.data.items)
    .filter((message) => message.metadata?.delegated_child !== true)
    // 工具轮次和委派终态观察属于持久化执行记录，并非聊天气泡；它们由活动时间线和子 Agent 侧栏展示。
    .filter((message) => message.role !== 'tool')
    .filter((message) => message.metadata?.runtime_run_id == null)
    .filter((message) => message.metadata?.delegated_result_revision !== true)
    .filter((message) => !(message.role === 'assistant' && Array.isArray(message.metadata?.tool_calls)))
  const conversationTurns = buildConversationTurnSummaries(visibleMessages)
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
  // 终态消息一旦刷新到位，立即隐藏 live item；这样持久正文与流式正文不会同时出现一帧。
  const liveReplyPersisted = Boolean(liveRun.runId && repliedRunIds.has(liveRun.runId))
  const completedThoughtLayoutVersion = Object.entries(completedThoughtsByRun)
    .map(([runId, timeline]) => `${runId}:${timeline.elapsedMs}:${timeline.tools.length}:${timeline.items?.length ?? 0}`)
    .join('|')
  // 子任务、队友和选中子任务运行共同驱动右侧 ChildAgentPanel。
  const visibleChildTasks = childTasks.data.ownerSessionId === activeId ? childTasks.data.items : noDelegatedTasks
  const visibleTeammates = activeId ? teammates.data : noTeammates
  const visibleDurableTasks = durableTasks.data.filter((task) => task.session_id === activeId)
  const resumableTask = durableTask.data?.session_id === activeId && ['paused', 'needs_recovery', 'blocked'].includes(durableTask.data.status) ? durableTask.data : null
  const resumeFromComposer = !draftActive && !canInterrupt && !composerHasValue && !pendingAttachments.length && resumableTask !== null
  const childPanelOpen = childPanelState.open
  const setChildPanelOpen = useCallback((update: SetStateAction<boolean>) => {
    setChildPanelState((current) => {
      const open = typeof update === 'function' ? update(current.open) : update
      return { ...current, open }
    })
  }, [])
  const openFileChange = useCallback((selection: FileChangeSelection) => {
    setFileChangeSelection(selection)
    setChildPanelOpen(false)
  }, [setChildPanelOpen])
  const sidePanelOpen = childPanelOpen || Boolean(fileChangeSelection)
  const chatShellStyle = sidePanelWidth === null
    ? undefined
    : { '--session-side-panel-width': `${sidePanelWidth}px` } as CSSProperties

  function resolveSidePanelResizeBounds(): SidePanelResizeBounds {
    const shell = chatShellRef.current
    if (!shell) return { min: SIDE_PANEL_MIN_WIDTH, max: SIDE_PANEL_MAX_WIDTH }
    const sessionList = shell.querySelector<HTMLElement>('.session-list')
    const availableWidth = shell.clientWidth - (sessionList?.offsetWidth ?? 226) - CONVERSATION_MIN_WIDTH
    return { min: SIDE_PANEL_MIN_WIDTH, max: Math.max(SIDE_PANEL_MIN_WIDTH, Math.min(SIDE_PANEL_MAX_WIDTH, availableWidth)) }
  }

  function readSidePanelWidth(): number {
    const shell = chatShellRef.current
    const cssWidth = shell ? Number.parseFloat(getComputedStyle(shell).getPropertyValue('--session-side-panel-width')) : Number.NaN
    return Number.isFinite(cssWidth) ? cssWidth : SIDE_PANEL_DEFAULT_WIDTH
  }

  function handleSidePanelPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return
    const bounds = resolveSidePanelResizeBounds()
    const startWidth = sidePanelWidth ?? readSidePanelWidth()
    sidePanelResizeRef.current = { ...bounds, pointerId: event.pointerId, startX: event.clientX, startWidth }
    setSidePanelResizeBounds(bounds)
    setSidePanelResizing(true)
    event.currentTarget.setPointerCapture(event.pointerId)
    event.preventDefault()
  }

  function handleSidePanelPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const interaction = sidePanelResizeRef.current
    if (!interaction || interaction.pointerId !== event.pointerId) return
    setSidePanelWidth(sidePanelWidthAfterDrag(interaction.startWidth, interaction.startX, event.clientX, interaction))
  }

  function finishSidePanelPointerResize(event: ReactPointerEvent<HTMLDivElement>) {
    const interaction = sidePanelResizeRef.current
    if (!interaction || interaction.pointerId !== event.pointerId) return
    sidePanelResizeRef.current = null
    setSidePanelResizing(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }

  function handleSidePanelKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
    const bounds = resolveSidePanelResizeBounds()
    const currentWidth = sidePanelWidth ?? readSidePanelWidth()
    const delta = event.shiftKey ? 32 : 16
    const nextWidth = currentWidth + (event.key === 'ArrowLeft' ? delta : -delta)
    setSidePanelResizeBounds(bounds)
    setSidePanelWidth(clampSidePanelWidth(nextWidth, bounds))
    event.preventDefault()
  }

  // 窗口缩放后重新计算可用空间，避免已拖宽的侧栏挤压对话区或造成横向溢出。
  useLayoutEffect(() => {
    if (!sidePanelOpen) return
    const syncSidePanelBounds = () => {
      const bounds = resolveSidePanelResizeBounds()
      setSidePanelResizeBounds(bounds)
      setSidePanelWidth((current) => current === null ? current : clampSidePanelWidth(current, bounds))
    }
    syncSidePanelBounds()
    window.addEventListener('resize', syncSidePanelBounds)
    return () => window.removeEventListener('resize', syncSidePanelBounds)
  }, [sidePanelOpen])

  const sessionNavigation = buildSessionNavigation(workspaces.data, mergePendingSession(sessions.data, pendingSession))
  const activeChildTask = visibleChildTasks.find((task) => task.id === selectedChildTaskId) ?? visibleChildTasks[0]
  const childTaskRunId = stringId(activeChildTask?.child_run_id) || stringId(activeChildTask?.result?.child_run_id)
  const childTaskRun = childTaskRunId ? sessionRuns.find((run) => run.id === childTaskRunId) : undefined
  const childTaskEvents = useApiData<RunEvent[]>([], () => childTaskRunId
    ? api.list<RunEvent>(`/api/runs/${encodeURIComponent(childTaskRunId)}/events`, ['events'])
    : Promise.resolve([]), [childTaskRunId])
  // 审批必须按当前等待运行逐组读取，再合并为页面可见列表。
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
  const activeComposerSurface = composerSurface(visibleApprovals.length, draftActive)
  // 为运输 Hook 提供稳定命名的刷新/状态写入函数，使终态同步不依赖页面实现细节。
  const refreshMessages = messages.refresh
  const refreshRuns = runs.refresh
  const refreshChildTasks = childTasks.refresh
  const refreshTeammates = teammates.refresh
  const refreshContext = context.refresh
  const setRunsState = runs.setState
  const setApprovalsState = approvals.setState
  const settingsLocked = activeRunCanStream || !['idle', 'terminal'].includes(liveRun.status)
  const currentMenuContextKey = `${draftActive ? 'draft' : activeId}:${activeRunId}:${liveRun.runId}:${liveRun.status}`
  const menusLocked = settingsLocked || sending
  const visibleSettingsMenuOpen = resolveMenuOpen(settingsMenuOpen, openedMenuContextKey, currentMenuContextKey, menusLocked)
  const visibleAddMenuOpen = resolveMenuOpen(addMenuOpen, openedMenuContextKey, currentMenuContextKey, menusLocked)
  const visiblePermissionMenuOpen = resolveMenuOpen(permissionMenuOpen, openedMenuContextKey, currentMenuContextKey, menusLocked)

  useEffect(() => {
    if (!visibleSettingsMenuOpen) return
    const closeOnPointerDown = (event: PointerEvent) => {
      if (!settingsMenuRef.current?.contains(event.target as Node)) closeSettingsMenu()
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        closeSettingsMenu()
        window.requestAnimationFrame(() => settingsTriggerRef.current?.focus())
      }
    }
    document.addEventListener('pointerdown', closeOnPointerDown)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnPointerDown)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [visibleSettingsMenuOpen])

  useEffect(() => {
    if (!visibleAddMenuOpen && !visiblePermissionMenuOpen) return
    const closeOnPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (visibleAddMenuOpen && !addMenuRef.current?.contains(target)) closeCapabilityMenus()
      if (visiblePermissionMenuOpen && !permissionMenuRef.current?.contains(target)) closeCapabilityMenus()
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeCapabilityMenus()
    }
    document.addEventListener('pointerdown', closeOnPointerDown)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnPointerDown)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [visibleAddMenuOpen, visiblePermissionMenuOpen])

  useLayoutEffect(() => {
    thoughtHydrationRegistryRef.current.reset()
    historyScrollSessionRef.current = activeId
    stickToBottomRef.current = true
  }, [activeId])

  useEffect(() => {
    if (!activeId || runs.loading || runs.data.ownerSessionId !== activeId) return
    let cancelled = false
    const finishedRunIds = new Set(
      runs.data.items
        .filter((run) => (!run.session_id || run.session_id === activeId) && isTerminalRunStatus(run.status))
        .map((run) => run.id),
    )
    // 消息是历史索引的权威来源；`/api/runs` 有分页上限，长会话不能只靠运行列表恢复思考时间线。
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
        // 事件接口按游标分页；完整读取后再生成思考时间线，避免长运行只水合最新一页。
        const events: RunEvent[] = []
        let before: number | undefined
        do {
          const page = await api.listRunEvents(runId, before === undefined ? {} : { before })
          events.push(...page.items)
          before = page.next_before === null ? undefined : page.next_before
        } while (before !== undefined && !cancelled && activeIdRef.current === activeId)
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

  function setExpandedWorkspaceIds(update: SetStateAction<Set<string>>) {
    setWorkspaceExpansion((current) => {
      const currentIds = resolveExpandedWorkspaceIds(current, workspaceExpansionContextKey, defaultExpandedWorkspaceId)
      return {
        contextKey: workspaceExpansionContextKey,
        ids: typeof update === 'function' ? update(currentIds) : update,
      }
    })
  }

  // 清除待上传附件及对应 input 值，保证再次选择同名文件仍会触发 change。
  function clearPendingAttachments() {
    pendingAttachmentsRef.current.forEach((item) => {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl)
    })
    pendingAttachmentsRef.current = []
    setPendingAttachments([])
    if (attachmentInputRef.current) attachmentInputRef.current.value = ''
  }

  // 校验新选择文件并与现有队列合并，同时为列表生成稳定本地 ID。
  function queueAttachments(files: FileList | null) {
    const selected = Array.from(files || [])
    if (!selected.length) return
    const current = pendingAttachmentsRef.current
    const result = selectAttachmentFiles(selected, current.map((item) => item.file))
    if (result.error) {
      setActionError(result.error)
      if (attachmentInputRef.current) attachmentInputRef.current.value = ''
      return
    }
    const added = result.files.map((file, index) => ({
      id: `attachment-${file.lastModified}-${file.size}-${index}-${Math.random().toString(36).slice(2)}`,
      file,
      previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : '',
    }))
    const next = [...current, ...added]
    pendingAttachmentsRef.current = next
    setPendingAttachments(next)
    setActionError('')
    if (attachmentInputRef.current) attachmentInputRef.current.value = ''
  }

  // 从状态和同步 ref 中同时移除附件，确保紧接着提交时读取到最新队列。
  function removePendingAttachment(id: string) {
    const target = pendingAttachmentsRef.current.find((item) => item.id === id)
    if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl)
    const next = pendingAttachmentsRef.current.filter((item) => item.id !== id)
    pendingAttachmentsRef.current = next
    setPendingAttachments(next)
  }

  // 将尚未创建的新会话恢复为初始草稿，并废弃此前异步操作版本。
  function clearDraftState() {
    draftVersionRef.current += 1
    clearPendingAttachments()
    setDraftActive(false)
    setDraftRootPath('')
    setDraftSettings(draftSettingsWithPermission(permissionSettings.data?.permission_mode ?? 'smart'))
    setLiveRun(emptyLiveRun())
    draftIdempotencyKeyRef.current = ''
  }

  // 进入新会话模式；传入项目时直接把草稿归属到该项目，避免依赖当前会话推断目录。
  function beginDraft(workspace?: Workspace) {
    if (sendingRef.current) return
    closeAllMenus()
    setPendingSession(null)
    clearPendingAttachments()
    const selectedProjectRoot = workspace ? (workspace.root_path || workspace.path || '') : projectRootForSession(workspaces.data, activeSession)
    const selectedProjectId = workspace?.id || (selectedProjectRoot ? activeSession?.workspace_id : '')
    draftVersionRef.current += 1
    setActionError('')
    setCompletedThoughtsByRun({})
    setInterruptedRunId('')
    setSelectedChildTaskId('')
    setChildPanelState((current) => ({ ...current, sessionId: '', autoOpened: false }))
    setHistoryHydration({ sessionId: '', complete: false })
    setDraftRootPath(selectedProjectRoot)
    setDraftSettings(draftSettingsWithPermission(permissionSettings.data?.permission_mode ?? 'smart'))
    setLiveRun(emptyLiveRun())
    draftIdempotencyKeyRef.current = createDraftIdempotencyKey()
    setExpandedWorkspaceIds(selectedProjectId ? new Set([selectedProjectId]) : new Set())
    setActiveId('')
    setDraftActive(true)
  }

  // 切换至持久化会话；后续 useApiData 依赖 activeId 自动加载关联资源。
  function openExistingSession(sessionId: string) {
    if (draftActive && sendingRef.current) return
    closeAllMenus()
    if (draftActive) {
      clearDraftState()
    }
    setActionError('')
    setCompletedThoughtsByRun({})
    setInterruptedRunId('')
    setSelectedChildTaskId('')
    setHistoryHydration({ sessionId, complete: false })
    setLiveRun(emptyLiveRun())
    setPendingSession(null)
    clearPendingAttachments()
    stickToBottomRef.current = true
    historyScrollSessionRef.current = sessionId
    setHistoryOpenVersion((version) => version + 1)
    setActiveId(sessionId)
  }

  // 在不可变 Set 中切换项目树展开状态。
  function toggleProject(workspaceId: string) {
    setExpandedWorkspaceIds((current) => {
      const next = new Set(current)
      if (next.has(workspaceId)) next.delete(workspaceId)
      else next.add(workspaceId)
      return next
    })
  }

  // 以项目行容器右边界计算悬浮卡片坐标，只避开最右侧删除按钮，并限制在视口内。
  function showProjectHoverCard(
    event: React.MouseEvent<HTMLButtonElement> | React.FocusEvent<HTMLButtonElement>,
    workspace: Workspace,
    conversationCount: number,
    path: string,
  ) {
    const rect = event.currentTarget.getBoundingClientRect()
    const rowRect = event.currentTarget.parentElement?.getBoundingClientRect()
    const cardWidth = 280
    const cardHeight = 118
    setProjectHoverCard({
      id: workspace.id,
      name: workspace.name,
      path,
      conversationCount,
      left: Math.min((rowRect?.right ?? rect.right) + 8, window.innerWidth - cardWidth - 12),
      top: Math.max(12, Math.min(rect.top - 4, window.innerHeight - cardHeight - 12)),
    })
  }

  // 调用系统文件夹选择器创建工作区，并刷新项目树。
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

  // 确认并删除项目；若项目内存在活动运行则阻止删除以免丢失进行中工作。
  async function deleteProject(workspace: Workspace, projectSessions: Session[]) {
    const workspaceId = stringId(workspace.id)
    const deletedSessionIds = new Set(projectSessions.map((session) => stringId(session.id)))
    const activeProjectRun = (sendingRef.current || canInterrupt)
      && projectSessions.some((session) => stringId(session.id) === activeIdRef.current)
    if (!workspaceId || deletingWorkspaceId || activeProjectRun) return
    if (!window.confirm(projectDeleteConfirmation(workspace.name, projectSessions.length))) return
    setDeletingWorkspaceId(workspaceId)
    setProjectError('')
    setProjectHoverCard(null)
    try {
      await api.delete<void>(`/api/workspaces/${encodeURIComponent(workspaceId)}`)
      const [, latestSessions] = await Promise.all([workspaces.refresh(), sessions.refresh()])
      setPendingSession((current) => pendingSessionAfterRemoval(current, deletedSessionIds))
      if (deletedSessionIds.has(activeIdRef.current)) {
        const nextSessionId = latestSessions?.[0] ? stringId(latestSessions[0].id) : ''
        closeRunTransport()
        terminalSyncVersionRef.current += 1
        setLiveRun(emptyLiveRun())
        setCompletedThoughtsByRun({})
        setInterruptedRunId('')
        setSelectedChildTaskId('')
        setHistoryHydration({ sessionId: nextSessionId, complete: false })
        setActiveId(nextSessionId)
      }
      setExpandedWorkspaceIds((current) => {
        const next = new Set(current)
        next.delete(workspaceId)
        return next
      })
    } catch (error) {
      setProjectError(describeError(error))
    } finally {
      setDeletingWorkspaceId('')
    }
  }

  // 为新会话选择或创建工作区，但暂不创建会话本身。
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

  // 只解除草稿与项目的关联，保留草稿输入和其他设置。
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
    refreshDurableTasks: refreshDurableTaskState,
  })

  useEffect(() => {
    closeRunTransport()
    if (pendingSessionSendRef.current?.sessionId !== activeId) pendingSessionSendRef.current = null
    terminalSyncVersionRef.current += 1
    seenStreamEventsRef.current = { runId: '', eventIds: new Set() }
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
    startRunStream(pending.runId, pending.sessionId, pending.startedAt)
    void refreshMessages()
    void refreshRuns()
    void refreshContext()
  }, [activeId, refreshContext, refreshMessages, refreshRuns, startRunStream, visibleMessages])

  useEffect(() => {
    if (!activeId || messages.loading || !activeRun?.id || !activeRunCanStream || liveRun.status === 'terminal') return
    if (streamRunIdRef.current === activeRun.id) return
    startRunStream(activeRun.id, activeId, activeRun.started_at)
  }, [activeId, activeRun?.id, activeRun?.started_at, activeRun?.status, activeRun?.stop_reason, activeRunCanStream, liveRun.status, messages.loading, startRunStream, visibleMessages])

  // 新打开的历史在消息和持久化思考/工具时间线均水合前保持锚定；程序布局滚动不能取消首次锚点。
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
      // 水合完成后一次跳到底部，不在长历史中播放滚动动画，以保持会话切换即时响应。
      observedElement = element
      element.addEventListener('scrollend', finishHistoryAnchor)
      element.scrollTo({ top: element.scrollHeight })
      settleFrame = window.requestAnimationFrame(finishHistoryAnchor)
    }
    // React 提交完整历史后再等待两个浏览器布局周期，确保最终滚动高度稳定。
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

  // 统一处理首轮建会话和已有会话续写，复用幂等键防止网络重试造成重复运行。
  async function submitMessage(content: string, files: File[], consumeComposer: boolean) {
    if (sendingRef.current || (!activeId && !draftActive) || (!content && !files.length) || settingsSaving || capabilitySaving || settingsLocked) return
    sendingRef.current = true
    closeAllMenus()
    setSending(true); setActionError('')
    setLastSubmittedContent(content)
    stickToBottomRef.current = true
    setLiveRun({ ...emptyLiveRun(), phase: '思考中…', status: 'connecting', thought: { ...emptyThoughtTimeline, startedAt: runThinkingStartedAt(undefined) }, thinkingStatus: pickThinkingStatus() })
    try {
      if (draftActive) {
        const draftVersion = draftVersionRef.current
        const idempotencyKey = draftIdempotencyKeyRef.current || createDraftIdempotencyKey()
        draftIdempotencyKeyRef.current = idempotencyKey
        const draftPayload = buildDraftLaunchPayload(
          idempotencyKey,
          content,
          draftRootPath,
          draftSettings,
        )
        if (!content && files[0]) draftPayload.title = draftSessionTitle(files[0].name)
        const launched = files.length
          ? await api.postForm<DraftLaunchResponse>('/api/drafts/launch-input', attachmentForm(draftPayload, files))
          : await api.post<DraftLaunchResponse>('/api/drafts/launch', draftPayload)
        if (draftVersionRef.current !== draftVersion) return
        const sessionId = stringId(launched.session?.id)
        const runId = stringId(launched.run?.id)
        if (!sessionId || !runId) throw new Error('草稿启动响应缺少会话或运行标识。')
        setPendingSession(launched.session)
        setInterruptedRunId('')
        pendingDraftRunRef.current = { sessionId, runId, startedAt: launched.run.started_at }
        if (consumeComposer) composerInputRef.current?.clear()
        clearDraftState()
        setActiveId(sessionId)
        void sessions.refresh()
        if (draftRootPath || launched.workspace) void workspaces.refresh()
        return
      }

      const targetSessionId = activeId
      const pendingSend = pendingSessionSendRef.current
      const fileSignature = attachmentSignature(files)
      const idempotencyKey = pendingSend?.sessionId === targetSessionId && pendingSend.content === content && pendingSend.attachmentSignature === fileSignature
        ? pendingSend.key
        : createTurnIdempotencyKey()
      pendingSessionSendRef.current = { sessionId: targetSessionId, content, attachmentSignature: fileSignature, key: idempotencyKey }
      const runPayload = {
        content,
        idempotency_key: idempotencyKey,
      }
      const launched = files.length
        ? await api.postForm<Run>(`/api/sessions/${targetSessionId}/turns`, attachmentForm(runPayload, files))
        : await api.post<Run>(`/api/sessions/${targetSessionId}/run`, runPayload)
      pendingSessionSendRef.current = null
      setInterruptedRunId('')
      if (consumeComposer) {
        composerInputRef.current?.clear()
        clearPendingAttachments()
      }
      void messages.refresh()
      void runs.refresh()
      void context.refresh()
      void refreshDurableTaskState()
      startRunStream(launched.id, targetSessionId, launched.started_at)
    } catch (error) {
      setLiveRun(emptyLiveRun())
      setActionError(describeError(error))
      if (!draftActive && activeId) {
        // 运输失败前请求可能已经提交；重新加载持久状态，避免已接收轮次只留下用户气泡。
        void Promise.all([messages.refresh(), runs.refresh(), context.refresh(), refreshDurableTaskState()])
      }
    } finally {
      sendingRef.current = false
      setSending(false)
    }
  }

  // 表单入口从非受控编辑器读取最新文本和附件快照，再交给 submitMessage。
  function sendMessage(event: FormEvent) {
    event.preventDefault()
    if (resumeFromComposer && resumableTask) {
      void resumeDurableTask(resumableTask.id)
      return
    }
    void submitMessage(
      composerInputRef.current?.getValue().trim() || '',
      pendingAttachmentsRef.current.map((item) => item.file),
      true,
    )
  }

  // 恢复是任务控制动作，不经过普通消息提交，也不消耗编辑器中的草稿。
  async function resumeDurableTask(taskId: string) {
    const sessionId = activeIdRef.current
    if (!sessionId || sendingRef.current) return
    sendingRef.current = true
    setSending(true)
    setActionError('')
    try {
      const run = await api.post<Run>(`/api/sessions/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(taskId)}/resume`)
      if (activeIdRef.current !== sessionId) return
      setInterruptedRunId('')
      startRunStream(run.id, sessionId, run.started_at)
      await Promise.all([refreshDurableTaskState(), runs.refresh()])
    } catch (error) {
      if (activeIdRef.current === sessionId) setActionError(describeError(error))
    } finally {
      sendingRef.current = false
      setSending(false)
    }
  }

  // 请求后端中断当前运行；实际草稿和终态随后由运输层事件同步。
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
      // 服务端会在现有流发布 `run_stopped`；保持运输开启，让部分回复和终态时间线走常规同步路径。
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

  // 取消持久任务并刷新卡片，避免仅在前端隐藏仍在执行的后台工作。
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
      await Promise.all([durableTask.refresh(), durableTasks.refresh(), runs.refresh()])
    } catch (error) {
      if (activeIdRef.current === sessionId) setActionError(describeError(error))
    } finally {
      setCancellingTaskId('')
    }
  }

  // 把用户中断后的最后一次提交恢复进编辑器，供修改后重新发送。
  function editInterruptedPrompt() {
    const content = lastSubmittedContent.trim()
    if (!content || !canEditInterrupted) return
    composerInputRef.current?.setValue(content)
    setInterruptedRunId('')
    setLiveRun(emptyLiveRun())
    setActionError('')
    window.requestAnimationFrame(() => composerInputRef.current?.focus?.())
  }

  // 提交审批决策并同步运行、消息、上下文和子任务；批准时继续监听原运行。
  async function decideApproval(id: string, decision: 'approve' | 'reject', approvalRunId: string) {
    const approvalSessionId = activeIdRef.current
    if (!approvalSessionId || !approvalRunId || !sessionRuns.some((run) => run.id === approvalRunId)) return
    setActionError(''); setDecidingApproval(id)
    try {
      await api.post(`/api/approvals/${id}/decide`, { decision })
      if (activeIdRef.current !== approvalSessionId) return
      // 后端已完成审批并启动续跑；先释放当前卡的锁定，避免下一张卡等待整组刷新。
      setDecidingApproval('')
      setApprovalsState({
        data: removePendingApproval(approvals.data, id),
        loading: false,
        error: '',
      })
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

  // 更新模型、思考等级或单会话记忆开关；草稿模式仅更新本地设置。
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

  // 更新技能、MCP 和权限能力；草稿在首轮创建时随 launch payload 一并提交。
  async function updateSessionCapabilities(payload: { skill_ids?: string[]; mcp_server_names?: string[]; permission_mode?: PermissionMode }) {
    if (settingsLocked || sending || capabilitySaving) return
    if (draftActive) {
      if (payload.permission_mode !== undefined) {
        setCapabilitySaving(true); setActionError('')
        try {
          const result = await api.put<PermissionSettings>('/api/permissions/settings', { permission_mode: payload.permission_mode })
          permissionSettings.setState({ data: result, loading: false, error: '' })
          setDraftSettings((current) => ({ ...current, permission_mode: result.permission_mode }))
        } catch (error) {
          setActionError(describeError(error))
        } finally {
          setCapabilitySaving(false)
        }
        return
      }
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
    try {
      const updated = await api.patch<Session>(`/api/sessions/${activeId}`, payload)
      if (payload.permission_mode !== undefined) permissionSettings.setState({ data: { permission_mode: updated.permission_mode || payload.permission_mode }, loading: false, error: '' })
      await sessions.reload()
    }
    catch (error) { setActionError(describeError(error)) } finally { setCapabilitySaving(false) }
  }

  // 删除单个会话并选择剩余最近会话；进行中会话由调用处禁用删除入口。
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
      setPendingSession((current) => pendingSessionAfterRemoval(current, new Set([sessionId])))
      if (sessionId === activeIdRef.current) {
        const nextSessionId = latest?.[0] ? stringId(latest[0].id) : ''
        closeRunTransport()
        terminalSyncVersionRef.current += 1
        setLiveRun(emptyLiveRun())
        setCompletedThoughtsByRun({})
        setInterruptedRunId('')
        setSelectedChildTaskId('')
        setHistoryHydration({ sessionId: nextSessionId, complete: false })
        setActiveId(nextSessionId)
      }
    } catch (error) {
      setActionError(describeError(error))
    } finally {
      setDeletingSessionId('')
    }
  }

  // 生成项目树内复用的会话行，并把选择与删除动作绑定到对应 session.id。
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

  // 切换当前会话或草稿的技能 ID。
  function toggleSessionSkill(skillId: string) {
    void updateSessionCapabilities({ skill_ids: toggleSelectedId(selectedSessionSkillIds, skillId) })
  }

  // 切换当前会话或草稿按名称启用的 MCP 服务。
  function toggleSessionMcp(serverName: string) {
    void updateSessionCapabilities({ mcp_server_names: toggleSelectedId(selectedSessionMcpNames, serverName) })
  }

  // 保存权限模式并关闭对应弹出菜单。
  function selectPermissionMode(mode: PermissionMode) {
    closeCapabilityMenus()
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
  const effectiveModel = effectiveSettings.model
  const selectedThinkingValue = resolveEffectiveThinking(settingsSession?.thinking_level)
  const modelOptions = connections.data.filter((connection) => connection.enabled !== false).flatMap((connection) => {
    return availableConnectionModels(connection).map((model) => ({ value: `${connection.id}::${model}`, label: model, connection: connection.name }))
  })
  const selectedModelValue = effectiveSettings.selectedValue
  const thinkingButtonLabel = thinkingLevelLabels[selectedThinkingValue]
  const modelButtonLabel = shortModelLabel(effectiveModel)
  // 同时关闭设置主菜单和二级菜单，防止残留不可见焦点。
  function closeSettingsMenu() {
    setSettingsMenuOpen(false)
    setSettingsSubmenu(null)
    setOpenedMenuContextKey('')
  }
  function closeCapabilityMenus() {
    setAddMenuOpen(false)
    setSkillSubmenuOpen(false)
    setMcpSubmenuOpen(false)
    setPermissionMenuOpen(false)
    setOpenedMenuContextKey('')
  }
  function closeAllMenus() {
    setSettingsMenuOpen(false)
    setSettingsSubmenu(null)
    setAddMenuOpen(false)
    setSkillSubmenuOpen(false)
    setMcpSubmenuOpen(false)
    setPermissionMenuOpen(false)
    setOpenedMenuContextKey('')
  }
  function toggleSettingsMenu() {
    const nextOpen = !visibleSettingsMenuOpen
    closeCapabilityMenus()
    setOpenedMenuContextKey(nextOpen ? currentMenuContextKey : '')
    setSettingsMenuOpen(nextOpen)
    setSettingsSubmenu(null)
  }
  function toggleAddMenu() {
    const nextOpen = !visibleAddMenuOpen
    setSettingsMenuOpen(false)
    setSettingsSubmenu(null)
    setPermissionMenuOpen(false)
    setOpenedMenuContextKey(nextOpen ? currentMenuContextKey : '')
    setAddMenuOpen(nextOpen)
    setSkillSubmenuOpen(false)
    setMcpSubmenuOpen(false)
  }
  function togglePermissionMenu() {
    const nextOpen = !visiblePermissionMenuOpen
    setSettingsMenuOpen(false)
    setSettingsSubmenu(null)
    setAddMenuOpen(false)
    setSkillSubmenuOpen(false)
    setMcpSubmenuOpen(false)
    setOpenedMenuContextKey(nextOpen ? currentMenuContextKey : '')
    setPermissionMenuOpen(nextOpen)
  }
  // 将组合选择值转换成连接/模型字段后保存。
  function selectModel(value: string) {
    closeSettingsMenu()
    void updateSessionSettings(modelSelectionPayload(value))
  }
  // 保存用户实际选择的四档思考强度，下一次普通 Run 将直接读取该会话值。
  function selectThinking(value: ThinkingLevel) {
    closeSettingsMenu()
    void updateSessionSettings({ thinking_level: value })
  }
  // 切换单会话记忆召回；全局关闭时 UI 会禁用该入口。
  function toggleSessionMemories() {
    closeSettingsMenu()
    void updateSessionSettings({ use_memories: !selectedUseMemories })
  }
  // 打开模型或思考二级菜单，并可在键盘导航时聚焦首个选项。
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
      <div ref={chatShellRef} className={`chat-shell ${sidePanelOpen ? 'child-panel-open' : ''}${sidePanelResizing ? ' is-resizing' : ''}`} style={chatShellStyle}>
          <aside className="session-list project-session-sidebar" onScroll={() => setProjectHoverCard(null)}>
            <section className="draft-tree-section">
              <button type="button" className="new-draft-button" disabled={sending} onClick={() => beginDraft()}><Plus size={14} />新建对话</button>
            </section>
            <section className="project-tree-section">
              <header className="sidebar-section-heading"><strong>项目</strong><button type="button" aria-label="从文件夹添加项目" title="从文件夹添加项目" disabled={addingProject || (draftActive && sending)} onClick={() => void addProjectFromFolder()}>{addingProject ? <LoaderCircle className="spin" size={14} /> : <Plus size={15} />}</button></header>
              {projectError && <div className="sidebar-inline-error"><AlertCircle size={13} /><span>{projectError}</span></div>}
              {sessionNavigation.projects.map(({ workspace, sessions: projectSessions }) => {
                const expanded = expandedWorkspaceIds.has(workspace.id)
                const path = workspace.root_path || workspace.path || '未提供路径'
                const deleting = deletingWorkspaceId === workspace.id
                const activeProjectRun = (sending || canInterrupt) && projectSessions.some((session) => stringId(session.id) === activeId)
                return <ProjectTreeItem
                  key={workspace.id}
                  workspace={workspace}
                  expanded={expanded}
                  deleting={deleting}
                  deleteDisabled={deleting || !!deletingWorkspaceId || activeProjectRun}
                  newConversationDisabled={sending || !!deletingWorkspaceId}
                  hoverCardVisible={projectHoverCard?.id === workspace.id}
                  onToggle={() => toggleProject(workspace.id)}
                  onNewConversation={() => beginDraft(workspace)}
                  onDelete={() => void deleteProject(workspace, projectSessions)}
                  onShowHoverCard={(event) => showProjectHoverCard(event, workspace, projectSessions.length, path)}
                  onHideHoverCard={() => setProjectHoverCard((current) => current?.id === workspace.id ? null : current)}
                >
                  <div className="project-children">
                    {projectSessions.length ? projectSessions.map((session) => renderSessionTreeItem(session)) : <p className="tree-empty">暂无对话</p>}
                  </div>
                </ProjectTreeItem>
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
          <section className={`conversation ${sidePanelOpen ? 'with-child-panel' : ''}`}>
            {(activeSession || draftActive) && <button type="button" className="child-panel-toggle conversation-side-toggle" aria-label={fileChangeSelection ? '关闭文件变更面板' : childPanelOpen ? '收起子 Agent 面板' : '打开子 Agent 面板'} title={fileChangeSelection ? '关闭文件变更面板' : childPanelOpen ? '收起子 Agent 面板' : '打开子 Agent 面板'} aria-expanded={sidePanelOpen} onClick={() => fileChangeSelection ? setFileChangeSelection(null) : setChildPanelOpen((open) => !open)}>{fileChangeSelection ? <X size={14} /> : childPanelOpen ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}</button>}
            <ConversationTurnTimeline turns={conversationTurns} />
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
                  return <MessageBubble key={message.id} message={message} thoughtRunId={messageRunId || undefined} thoughtTimeline={completedThought} onOpenFileChange={openFileChange} />
                }) : liveRun.status === 'idle' ? <EmptyState icon={MessageSquare} title="从一条清晰的任务开始" description="描述目标、约束和期望产物，Agent 会先理解上下文再行动。" /> : null}
                {!draftActive && messages.error && !!visibleMessages.length && <p className="inline-error" role="alert">消息同步失败：{messages.error}</p>}
                {!draftActive && stoppedRunNotices.map((run) => <div key={`run-notice:${run.id}`} className="stopped-run-notice" role="status"><AlertCircle size={16} /><div><strong>{run.status === 'failed' ? '本次运行失败，未生成最终回复' : '本次运行已停止，未生成最终回复'}</strong><p>{run.error_message || run.stop_reason || 'Agent 未能继续执行，请调整指令后重试。'}</p></div></div>)}
                {liveRun.status !== 'idle' && !liveReplyPersisted
                  && (!completedThoughtsByRun[liveRun.runId] || (canEditInterrupted && liveRun.runId === interruptedRunId))
                  && <LiveAssistantMessage liveRun={liveRun} onOpenFileChange={openFileChange} />}
                {canEditInterrupted && <div className="interrupted-run-actions">
                  <button type="button" className="interrupted-edit-button" onClick={editInterruptedPrompt}>
                    <Pencil size={12} />重新编辑本次输入
                  </button>
                  <span>已保留当前部分输出，重新发送前不会写入新的上下文。</span>
                </div>}
                {!draftActive && approvals.error && <ErrorState message={`审批状态读取失败：${approvals.error}`} onRetry={approvals.reload} />}
                {!draftActive && approvals.loading && activeRunId && !visibleApprovals.length && liveRun.status === 'awaiting_approval' && <LoadingState label="正在读取审批状态" />}
              </div>
              <div className={`composer-stage composer-stage-${activeComposerSurface}`} aria-live="polite">
              <form className={`composer composer-surface composer-surface-composer${activeComposerSurface === 'composer' ? ' is-active' : ''}`} aria-hidden={activeComposerSurface !== 'composer'} inert={activeComposerSurface !== 'composer' || undefined} onSubmit={sendMessage}>
                {actionError && <p className="form-error" role="alert">{actionError}</p>}
                {draftActive && <div className="draft-project-controls">
                  <button type="button" className="draft-project-button" disabled={pickingDraftProject || sending} onClick={() => void selectDraftProject()}>{pickingDraftProject ? <LoaderCircle className="spin" size={13} /> : <FolderOpen size={13} />}选择项目</button>
                  {draftRootPath && <span className="draft-folder-pill" title={draftRootPath}><Folder size={12} /><span>{folderName(draftRootPath)}</span><button type="button" aria-label="清除所选项目" disabled={sending} onClick={clearDraftProject}><X size={11} /></button></span>}
                </div>}
                {!!pendingAttachments.length && <div className="attachment-tray" role="list" aria-label="待发送附件">
                  {pendingAttachments.map((item) => <div className="attachment-chip" role="listitem" key={item.id}>
                    <span className="attachment-chip-preview">{item.previewUrl ? <img src={item.previewUrl} alt="" /> : <FileText size={16} />}</span>
                    <span className="attachment-chip-copy"><strong title={item.file.name}>{item.file.name}</strong><small>仅本会话 · {formatAttachmentSize(item.file.size)}</small></span>
                    <button type="button" aria-label={`移除附件 ${item.file.name}`} title="移除附件" disabled={sending} onClick={() => removePendingAttachment(item.id)}><X size={12} /></button>
                  </div>)}
                </div>}
                <input ref={attachmentInputRef} className="attachment-file-input" type="file" multiple tabIndex={-1} aria-hidden="true" onChange={(event) => queueAttachments(event.currentTarget.files)} />
                <ComposerTextArea ref={composerInputRef} disabled={draftActive && sending} resetKey={`${activeId}:${draftActive}`} placeholder={draftActive ? '描述你想完成的任务……' : '告诉 PGAgent 你想完成什么……'} onHasValueChange={setComposerHasValue} />
                <div className="composer-toolbar">
                  <div className="composer-left-actions">
                    <button type="button" className="composer-tool-button composer-plus-button attachment-trigger" aria-label="添加本机附件" title="添加本机附件（仅当前会话可见）" disabled={settingsLocked || sending} onClick={() => attachmentInputRef.current?.click()}>
                      <Paperclip size={14} />
                      {!!pendingAttachments.length && <b>{pendingAttachments.length}</b>}
                    </button>
                    <div className="session-capability-picker" ref={addMenuRef}>
                      <button type="button" className="composer-tool-button composer-plus-button" aria-label="添加能力" aria-haspopup="menu" aria-expanded={visibleAddMenuOpen} aria-busy={capabilitySaving} disabled={settingsLocked || sending || capabilitySaving} onClick={toggleAddMenu}>
                        <Plus size={15} />
                        {!!(selectedSessionSkillIds.length + selectedSessionMcpNames.length) && <b>{selectedSessionSkillIds.length + selectedSessionMcpNames.length}</b>}
                      </button>
                      {visibleAddMenuOpen && <div className="capability-popover capability-level-two" role="menu" aria-label="添加能力">
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
                        <button type="button" className={mcpSubmenuOpen ? 'active' : ''} role="menuitem" aria-haspopup="menu" aria-expanded={mcpSubmenuOpen} onMouseEnter={() => { setMcpSubmenuOpen(true); setSkillSubmenuOpen(false) }} onClick={() => { setMcpSubmenuOpen((open) => !open); setSkillSubmenuOpen(false) }}><Cable size={14} /><span>MCP</span><small>{selectedSessionMcpNames.length ? `已选 ${selectedSessionMcpNames.length}` : '未选择'}</small><ChevronRight size={13} /></button>
                        {mcpSubmenuOpen && <div className="capability-popover capability-level-three" role="menu" aria-label="选择 MCP">
                          <p>当前会话使用的 MCP</p>
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
                      <button type="button" className={`composer-tool-button permission-trigger${selectedPermissionMode === 'full' ? ' permission-trigger-full' : ''}`} aria-label={`权限模式：${permissionLabel(selectedPermissionMode)}`} aria-haspopup="menu" aria-expanded={visiblePermissionMenuOpen} disabled={settingsLocked || sending || capabilitySaving} onClick={togglePermissionMenu}>{selectedPermissionMode === 'full' ? <ShieldAlert size={14} /> : <ShieldCheck size={14} />}<span>{permissionLabel(selectedPermissionMode)}</span><ChevronRight size={12} /></button>
                      {visiblePermissionMenuOpen && <div className="capability-popover permission-popover" role="menu" aria-label="权限模式">
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
                        aria-label={`当前模型 ${modelButtonLabel}，思考强度 ${thinkingButtonLabel}。点击更改`}
                        aria-haspopup="menu"
                        aria-expanded={visibleSettingsMenuOpen}
                        aria-busy={settingsSaving}
                        disabled={settingsSaving || settingsLocked || sending}
                        onClick={toggleSettingsMenu}
                      >
                        <span>{modelButtonLabel}</span><strong>{thinkingButtonLabel}</strong><ChevronRight size={12} aria-hidden="true" />
                      </button>
                      {visibleSettingsMenuOpen && <div className="session-settings-popover" role="menu" aria-label="模型和思考设置">
                        <button type="button" className="session-memory-setting" role="menuitemcheckbox" aria-checked={selectedUseMemories} aria-busy={settingsSaving} disabled={globalMemoriesDisabled || settingsSaving || settingsLocked || sending} onClick={toggleSessionMemories}>
                          <span><strong>使用已有记忆</strong><small>{globalMemoriesDisabled ? '全局已关闭' : selectedUseMemories ? '此会话会使用已有记忆' : '此会话不会使用已有记忆'}</small></span>
                          <span className="session-memory-switch" aria-hidden="true"><span /></span>
                        </button>
                        <button type="button" className={settingsSubmenu === 'model' ? 'active' : ''} role="menuitem" aria-haspopup="menu" aria-expanded={settingsSubmenu === 'model'} onMouseEnter={() => openSettingsSubmenu('model')} onKeyDown={(event) => { if (event.key === 'ArrowRight') { event.preventDefault(); openSettingsSubmenu('model', true) } }} onClick={() => openSettingsSubmenu('model', true)}>
                          <span>模型</span><small>{modelButtonLabel}</small><ChevronRight size={13} aria-hidden="true" />
                        </button>
                        {settingsSubmenu === 'model' && <div className="session-settings-submenu" ref={modelSubmenuRef} role="menu" aria-label="选择模型">
                          <p>模型</p>
                          {!modelOptions.length && <div className="session-settings-empty" role="status">暂无启用模型</div>}
                          {modelOptions.map((option, index) => <button key={`${option.value}:${index}`} type="button" role="menuitemradio" aria-checked={selectedModelValue === option.value} className={selectedModelValue === option.value ? 'selected' : ''} onClick={() => selectModel(option.value)}>
                            <span><strong>{option.label}</strong><small>{option.connection}</small></span><Check className="selection-check" size={14} aria-hidden="true" />
                          </button>)}
                        </div>}
                        <button type="button" className={settingsSubmenu === 'thinking' ? 'active' : ''} role="menuitem" aria-haspopup="menu" aria-expanded={settingsSubmenu === 'thinking'} onMouseEnter={() => openSettingsSubmenu('thinking')} onKeyDown={(event) => { if (event.key === 'ArrowRight') { event.preventDefault(); openSettingsSubmenu('thinking', true) } }} onClick={() => openSettingsSubmenu('thinking', true)}>
                          <span>推理强度</span><small>{thinkingButtonLabel}</small><ChevronRight size={13} aria-hidden="true" />
                        </button>
                        {settingsSubmenu === 'thinking' && <div className="session-settings-submenu" ref={thinkingSubmenuRef} role="menu" aria-label="选择推理强度">
                          <p>推理强度</p>
                          {sessionThinkingOptions.map((option) => <button key={option.value} type="button" role="menuitemradio" aria-checked={selectedThinkingValue === option.value} className={selectedThinkingValue === option.value ? 'selected' : ''} onClick={() => selectThinking(option.value)}>
                            <span><strong>{option.label}</strong>{option.hint && <small>{option.hint}</small>}</span><Check className="selection-check" size={14} aria-hidden="true" />
                          </button>)}
                        </div>}
                      </div>}
                    </div>
                    {draftActive ? <ContextUsageRing context={emptyDraftContext} /> : context.error ? <button type="button" className="context-state context-error" aria-label="上下文占用读取失败，点击重试" title={context.error} onClick={() => void context.reload()}><AlertCircle size={15} /></button> : context.loading || !context.data ? <span className="context-state" role="status" aria-label="正在读取上下文占用"><LoaderCircle className="spin" size={15} /></span> : <ContextUsageRing context={context.data} />}
                    <button
                      type={canInterrupt ? 'button' : 'submit'}
                      className={`send-button ${canInterrupt ? 'is-stop' : ''}`}
                      aria-label={canInterrupt ? '中断当前任务' : resumeFromComposer ? '继续任务' : '发送'}
                      title={canInterrupt ? '中断当前任务' : resumeFromComposer ? '继续任务' : '发送'}
                      aria-busy={Boolean(stoppingRunId)}
                      disabled={canInterrupt
                        ? Boolean(stoppingRunId)
                        : sending || settingsSaving || capabilitySaving || settingsLocked || (!resumeFromComposer && !composerHasValue && !pendingAttachments.length)}
                      onClick={canInterrupt ? () => void stopActiveRun() : undefined}
                    >
                      {canInterrupt
                        ? stoppingRunId ? <LoaderCircle className="spin" size={14} /> : <Square className="send-stop-icon" size={12} strokeWidth={3} fill="currentColor" />
                        : resumeFromComposer ? <span aria-hidden="true">▶</span> : <ArrowUp className="send-arrow-icon" size={16} strokeWidth={2.4} />}
                    </button>
                  </div>
                </div>
              </form>
              <section className={`composer composer-surface composer-surface-approval${activeComposerSurface === 'approval' ? ' is-active' : ''}`} aria-hidden={activeComposerSurface !== 'approval'} inert={activeComposerSurface !== 'approval' || undefined} aria-label="待审批操作">
                <div className="approval-composer-list">
                  {visibleApprovals.map((approval) => <ApprovalCard key={approval.id} approval={approval} embedded deciding={decidingApproval === approval.id} onDecision={decideApproval} />)}
                </div>
              </section>
              </div>
            </> : <EmptyState icon={MessageSquare} title="开始新对话" description="创建一个临时草稿；首次发送后才会保存为任务或项目对话。" action={<button className="button button-primary" onClick={() => beginDraft()}>新建对话</button>} />}
          </section>
          {sidePanelOpen && <div
            className={`side-panel-resizer${sidePanelResizing ? ' is-dragging' : ''}`}
            role="separator"
            tabIndex={0}
            aria-label="调整侧栏宽度"
            aria-orientation="vertical"
            aria-valuemin={sidePanelResizeBounds.min}
            aria-valuemax={sidePanelResizeBounds.max}
            aria-valuenow={Math.round(sidePanelWidth ?? readSidePanelWidth())}
            onPointerDown={handleSidePanelPointerDown}
            onPointerMove={handleSidePanelPointerMove}
            onPointerUp={finishSidePanelPointerResize}
            onPointerCancel={finishSidePanelPointerResize}
            onKeyDown={handleSidePanelKeyDown}
          />}
          {fileChangeSelection ? <FileChangePanel selection={fileChangeSelection} onClose={() => setFileChangeSelection(null)} /> : <ChildAgentPanel
            open={childPanelOpen}
            tasks={visibleChildTasks}
            durableTasks={visibleDurableTasks}
            selectedDurableTaskId={selectedDurableTaskId}
            durableSourceOpen={durableSourceOpen}
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
            onToggleDurableSource={() => setDurableSourceOpen((open) => !open)}
            onSelectDurableTask={(taskId) => { setSelectedDurableTaskId(taskId); setDurableSourceOpen(true) }}
            onResumeDurableTask={(taskId) => { void resumeDurableTask(taskId) }}
            onCancelDurableTask={(taskId) => { void cancelDurableTask(taskId) }}
            cancellingDurableTaskId={cancellingTaskId}
            resumingDurableTask={sending}
            onRetry={() => { void childTasks.reload(); void teammates.reload(); void childTaskEvents.reload() }}
          />}
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


const SessionsPageState = { nextSelectedSessionId, resolveActiveSessionId, nextChildPanelStateForTasks, resolveExpandedWorkspaceIds, resolveMenuOpen, clampSidePanelWidth, sidePanelWidthAfterDrag }

export { SessionsPage, SessionsPageState }
