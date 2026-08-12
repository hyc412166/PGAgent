import {
  Activity,
  AlertCircle,
  ArrowRight,
  Bot,
  BookOpen,
  Box,
  CalendarDays,
  Check,
  CheckCircle2,
  ChevronRight,
  ChartNoAxesCombined,
  Database,
  Download,
  FolderOpen,
  Folder,
  History,
  KeyRound,
  LayoutDashboard,
  LoaderCircle,
  Menu,
  MessageSquare,
  Network,
  PanelRightClose,
  PanelRightOpen,
  PanelLeftClose,
  Play,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Send,
  Settings2,
  ShieldCheck,
  Sparkles,
  SquareTerminal,
  Trash2,
  Upload,
  Users,
  Wrench,
  Workflow,
  X,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import { Fragment, type FormEvent, type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { ApiError, api, apiUrl, describeError } from './api'
import { permissionLabel, permissionOptions, toggleSelectedId } from './capabilitySelection'
import { modelSelectionPayload, resolveEffectiveThinking, shortModelLabel, thinkingLevelLabels } from './composerSettings'
import { fallbackMarketCategories, leaderboardRefreshDelayMs, marketCategoryDefinitions, normalizeMarketCategories } from './skillMarketCategories'
import { buildContextUsageView } from './contextUsage'
import { buildDraftLaunchPayload, createDraftIdempotencyKey } from './draftLaunch'
import { availableConnectionModels, resolveEffectiveModelSettings } from './modelSettings'
import { buildSessionNavigation, folderName, isDefaultWorkspace, projectRootForSession } from './sessionNavigation'
import { appendAssistantDelta, isTerminalRunStatus, isTerminalRunStreamEvent, parseRunStreamEvent, rememberRunStreamEvent, runStatusPhase, runStreamPhase, shouldRefreshConversationAfterApprovalDecision, visibleSessionItems, type RunStreamEvent } from './sessionStream'
import { emptyThoughtTimeline, formatLiveThinkingDuration, formatThoughtDuration, hasVisibleCompletedThought, pickThinkingStatus, thinkingStatusForRun, timelineFromRunEvents, updateThoughtTimeline, type ThoughtTimelineState } from './thoughtTimeline'
import { usageDateKey, usageDateOptions, usageDatePresetBounds, usageDateRange, usageRangeLabel, type QuickUsageDatePreset, type UsageDatePreset } from './usageDateRange'
import type {
  AgentProfile,
  Approval,
  Connection,
  DashboardData,
  DelegatedTask,
  Health,
  Message,
  Run,
  RunEvent,
  Session,
  SessionContext,
  TeamTask,
  ThinkingLevel,
  UsageBreakdownItem,
  UsageSession,
  UsageSummary,
  UsageWorkspace,
  ModelUsage,
  FolderSelection,
  PermissionMode,
  SkillCatalogItem,
  SkillInstallPreview,
  SkillMarketplaceBrowse,
  SkillMarketplaceCategory,
  SkillMarketplaceItem,
  SkillMarketplaceLeaderboards,
  SkillMarketplaceSearch,
  SkillMarketplaceView,
  ToolCatalogItem,
  Workspace,
} from './types'
import './App.css'

type LoadState<T> = { data: T; loading: boolean; error: string }
type LiveRunState = { runId: string; phase: string; draft: string; status: 'idle' | 'connecting' | 'live' | 'fallback' | 'awaiting_approval' | 'terminal'; error: string; thought: ThoughtTimelineState; thinkingStatus: string }
type OwnedSessionMessages = { ownerSessionId: string; items: Message[] }
type DraftSessionSettings = { model_connection_id: string | null; model_id: string | null; thinking_level: ThinkingLevel; skill_ids: string[]; permission_mode: PermissionMode }
type DraftLaunchResponse = { session: Session; run: Run; workspace?: Workspace }

const emptyDraftSettings: DraftSessionSettings = { model_connection_id: null, model_id: null, thinking_level: 'auto', skill_ids: [], permission_mode: 'smart' }
const emptyDraftContext: SessionContext = { used_tokens: 0, limit_tokens: 100_000, compact_threshold_tokens: 90_000, percent: 0 }

function emptyLiveRun(): LiveRunState {
  return { runId: '', phase: '', draft: '', status: 'idle', error: '', thought: emptyThoughtTimeline, thinkingStatus: '' }
}

const runStreamEventNames = [
  'run_state',
  'run_received',
  'context_prepared',
  'context_resumed',
  'context_compacted',
  'model_step_started',
  'model_retry',
  'assistant_delta',
  'tool_started',
  'tool_call',
  'tool_finished',
  'tool_result',
  'approval_requested',
  'approval_granted',
  'delegated_child_started',
  'delegated_child_continuation_started',
  'delegated_child_awaiting_approval',
  'delegated_child_completed',
  'delegated_child_stopped',
  'delegated_child_failed',
  'run_completed',
  'run_stopped',
  'model_failed',
  'integration_failed',
] as const

const activeRunStatuses = new Set(['received', 'running', 'preparing_context', 'planning', 'acting', 'observing', 'awaiting_approval'])

function useApiData<T>(initial: T, loader: () => Promise<T>, deps: readonly unknown[] = []) {
  const [state, setState] = useState<LoadState<T>>({ data: initial, loading: true, error: '' })
  const requestId = useRef(0)
  const hasLoaded = useRef(false)
  const isInitialLoad = !hasLoaded.current

  const reload = useCallback(async () => {
    const currentRequest = ++requestId.current
    setState((previous) => ({ ...previous, loading: true, error: '' }))
    try {
      const data = await loader()
      if (currentRequest === requestId.current) {
        hasLoaded.current = true
        setState({ data, loading: false, error: '' })
      }
    } catch (error) {
      if (currentRequest === requestId.current) setState((previous) => ({ ...previous, loading: false, error: describeError(error) }))
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  const refresh = useCallback(async () => {
    const currentRequest = ++requestId.current
    try {
      const data = await loader()
      if (currentRequest === requestId.current) {
        hasLoaded.current = true
        setState({ data, loading: false, error: '' })
        return data
      }
    } catch (error) {
      if (currentRequest === requestId.current && !hasLoaded.current) {
        setState((previous) => ({ ...previous, loading: false, error: describeError(error) }))
      }
      return undefined
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    void reload()
    return () => { requestId.current += 1 }
  }, [reload])

  return { ...state, initialLoading: state.loading && isInitialLoad, refreshing: state.loading && !isInitialLoad, reload, refresh, setState }
}

function formatDate(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

function stringId(value: unknown) {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : ''
}

function numberFromRecord(record: Record<string, unknown> | undefined, key: string) {
  const value = record?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

const statusText: Record<string, string> = {
  completed: '已完成',
  complete: '已完成',
  running: '运行中',
  acting: '执行中',
  planning: '规划中',
  preparing_context: '准备上下文',
  awaiting_approval: '等待审批',
  pending: '待处理',
  approved: '已批准',
  rejected: '已拒绝',
  failed: '失败',
  stopped: '已停止',
  queued: '排队中',
  blocked: '受阻',
  online: '在线',
  healthy: '正常',
  active: '活跃',
  idle: '空闲',
  default: '默认',
  todo: '待认领',
  in_progress: '进行中',
  review: '待验收',
}

function StatusBadge({ status = 'unknown' }: { status?: string }) {
  const normalized = status.toLowerCase()
  const tone = ['completed', 'complete', 'approved', 'online', 'healthy', 'active'].includes(normalized)
    ? 'success'
    : ['failed', 'rejected', 'blocked', 'stopped'].includes(normalized)
      ? 'danger'
      : ['running', 'acting', 'planning', 'preparing_context', 'in_progress'].includes(normalized)
        ? 'active'
        : ['awaiting_approval', 'pending', 'review'].includes(normalized)
          ? 'warning'
          : 'neutral'
  return <span className={`status status-${tone}`}><i aria-hidden="true" />{statusText[normalized] ?? status}</span>
}

function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="page-description">{description}</p>
      </div>
      {action && <div className="page-action">{action}</div>}
    </header>
  )
}

function EmptyState({ icon: Icon = Box, title, description, action }: { icon?: LucideIcon; title: string; description: string; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-icon"><Icon size={22} /></div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  )
}

function LoadingState({ label = '正在读取本地数据' }: { label?: string }) {
  return <div className="loading-state"><LoaderCircle className="spin" size={18} />{label}</div>
}

function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-state" role="alert">
      <AlertCircle size={19} />
      <div><strong>数据暂时不可用</strong><p>{message}</p></div>
      {onRetry && <button className="button button-quiet" onClick={onRetry}><RefreshCw size={15} />重试</button>}
    </div>
  )
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>
}

function CapabilityMultiSelect({
  label,
  items,
  selectedIds,
  loading,
  error,
  onRetry,
  onToggle,
}: {
  label: string
  items: Array<{ id: string; name: string; description?: string; enabled?: boolean }>
  selectedIds: string[]
  loading: boolean
  error: string
  onRetry: () => void
  onToggle: (id: string) => void
}) {
  return <section className="capability-select" aria-label={label}>
    <header><strong>{label}</strong><span>已选择 {selectedIds.length}</span></header>
    {error ? <div className="capability-inline-state error"><AlertCircle size={13} /><span>{error}</span><button type="button" onClick={onRetry}>重试</button></div>
      : loading ? <div className="capability-inline-state"><LoaderCircle className="spin" size={13} />正在读取…</div>
        : items.length ? <div className="capability-options">{items.map((item) => {
          const selected = selectedIds.includes(item.id)
          return <button key={item.id} type="button" className={selected ? 'selected' : ''} aria-pressed={selected} disabled={item.enabled === false} onClick={() => onToggle(item.id)}>
            <span className="capability-check">{selected && <Check size={12} />}</span>
            <span><strong>{item.name}</strong>{item.description && <small>{item.description}</small>}</span>
          </button>
        })}</div>
          : <p className="capability-empty">目录中暂无可用项目。</p>}
  </section>
}

function ContextUsageRing({ context }: { context: SessionContext }) {
  const view = buildContextUsageView(context)
  return (
    <div className={`context-usage context-${view.tone}`} tabIndex={0} role="img" aria-label={view.ariaLabel} aria-describedby="context-usage-tooltip">
      <span className="context-ring" aria-hidden="true">
        <svg viewBox="0 0 24 24">
          <circle className="context-ring-track" cx="12" cy="12" r="9" pathLength="100" />
          <circle className="context-ring-value" cx="12" cy="12" r="9" pathLength="100" strokeDasharray="100" strokeDashoffset={100 - view.percent} />
        </svg>
      </span>
      <div className="context-tooltip" id="context-usage-tooltip" role="tooltip">
        <strong>{view.roundedPercent}% 已用</strong>
        <p>{view.detail}</p>
        <small>{view.compressionHint}</small>
        {context.last_compacted_at && <small>上次压缩：{formatDate(context.last_compacted_at)}</small>}
      </div>
    </div>
  )
}

function SlidePanel({ title, description, onClose, children }: { title: string; description?: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose])

  return (
    <div className="overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="slide-panel" role="dialog" aria-modal="true" aria-labelledby="panel-title">
        <header>
          <div><p className="eyebrow">PGAgent 配置</p><h2 id="panel-title">{title}</h2>{description && <p>{description}</p>}</div>
          <button className="icon-button" onClick={onClose} aria-label="关闭"><X size={19} /></button>
        </header>
        {children}
      </section>
    </div>
  )
}

const navigation = [
  { path: '/dashboard', label: '总览', icon: LayoutDashboard },
  { path: '/agents', label: 'Agent', icon: Bot },
  { path: '/skills', label: '技能库', icon: BookOpen },
  { path: '/sessions', label: '会话', icon: MessageSquare },
  { path: '/runs', label: '运行记录', icon: History },
  { path: '/usage', label: '用量统计', icon: ChartNoAxesCombined },
  { path: '/settings/models', label: '模型设置', icon: Settings2 },
]

function AppShell() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const location = useLocation()
  const health = useApiData<Health | null>(null, () => api.get<Health>('/api/health'), [])

  useEffect(() => setMobileOpen(false), [location.pathname])

  return (
    <div className={`app-shell ${collapsed ? 'sidebar-collapsed' : ''}`}>
      <button className="mobile-menu" aria-label="打开导航" onClick={() => setMobileOpen(true)}><Menu /></button>
      {mobileOpen && <button className="mobile-backdrop" aria-label="关闭导航" onClick={() => setMobileOpen(false)} />}
      <aside className={`sidebar ${mobileOpen ? 'mobile-open' : ''}`}>
        <div className="brand">
          <div className="brand-mark" aria-hidden="true"><Sparkles size={20} /></div>
          <div className="brand-copy"><strong>PGAgent</strong><span>Local Workbench</span></div>
          <button className="collapse-button" onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? '展开导航' : '收起导航'}>
            <PanelLeftClose size={17} />
          </button>
        </div>
        <nav aria-label="主导航">
          <p className="nav-label">工作台</p>
          {navigation.slice(0, 6).map(({ path, label, icon: Icon }) => (
            <NavLink key={path} to={path} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`} title={collapsed ? label : undefined}>
              <Icon size={18} /><span>{label}</span>
            </NavLink>
          ))}
          <p className="nav-label nav-label-spaced">协作与系统</p>
          {navigation.slice(6).map(({ path, label, icon: Icon }) => (
            <NavLink key={path} to={path} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`} title={collapsed ? label : undefined}>
              <Icon size={18} /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className={`service-pill ${health.error ? 'offline' : ''}`} title={health.error || '本地服务已连接'}>
            <i />
            <div><strong>{health.error ? '服务未连接' : health.loading ? '正在检测' : '本地服务正常'}</strong><span>{health.data?.version ? `v${health.data.version}` : '127.0.0.1'}</span></div>
          </div>
        </div>
      </aside>
      <main className="main-content">
        <Routes>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/workspaces" element={<Navigate to="/sessions" replace />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/usage" element={<UsagePage />} />
          <Route path="/settings/models" element={<ModelsPage />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  )
}

function DashboardPage() {
  const navigate = useNavigate()
  const dashboard = useApiData<DashboardData>({}, () => api.get<DashboardData>('/api/dashboard'), [])
  const recentRuns = useApiData<Run[]>([], () => api.list<Run>('/api/runs?limit=5', ['runs']), [])
  const approvals = useApiData<Approval[]>([], () => api.list<Approval>('/api/approvals?status=pending', ['approvals']), [])
  const data = dashboard.data
  const displayedRuns = data.recent_runs ?? recentRuns.data
  const stats = [
    { label: '工作区', value: data.workspace_count ?? data.workspaces ?? 0, detail: '隔离的本地目录', icon: Folder, tone: 'orange' },
    { label: 'Agent', value: data.agent_count ?? data.agents ?? 0, detail: '可复用智能角色', icon: Bot, tone: 'violet' },
    { label: '活跃运行', value: data.active_runs ?? 0, detail: '正在处理的任务', icon: Activity, tone: 'green' },
    { label: '等待审批', value: data.pending_approvals ?? approvals.data.length, detail: '需人工确认的工具', icon: ShieldCheck, tone: 'yellow' },
  ]

  return (
    <div className="page">
      <PageHeader eyebrow="今天的工作状态" title="总览" description="从一个清晰的入口查看 Agent、运行与需要你处理的事项。" action={<button className="button button-primary" onClick={() => navigate('/sessions')}><Play size={16} />开始新任务</button>} />
      {dashboard.error && <ErrorState message={dashboard.error} onRetry={dashboard.reload} />}
      <section className="stat-grid" aria-label="关键指标">
        {stats.map(({ label, value, detail, icon: Icon, tone }) => (
          <article className="stat-card" key={label}>
            <div className={`stat-icon tone-${tone}`}><Icon size={19} /></div>
            <div><p>{label}</p><strong>{dashboard.loading ? '—' : value}</strong><span>{detail}</span></div>
          </article>
        ))}
      </section>
      <div className="dashboard-grid">
        <section className="card activity-card">
          <div className="card-heading"><div><p className="eyebrow">RUN STREAM</p><h2>最近运行</h2></div><button className="text-button" onClick={() => navigate('/runs')}>查看全部<ArrowRight size={15} /></button></div>
          {recentRuns.error ? <ErrorState message={recentRuns.error} onRetry={recentRuns.reload} /> : dashboard.loading || recentRuns.loading ? <LoadingState /> : displayedRuns.length ? (
            <div className="run-list">
              {displayedRuns.slice(0, 5).map((run) => <RunRow key={run.id} run={run} onClick={() => navigate('/runs')} />)}
            </div>
          ) : <EmptyState icon={Workflow} title="还没有运行记录" description="创建工作区和 Agent 后，发起第一条任务。" action={<button className="button button-secondary" onClick={() => navigate('/sessions')}>前往会话</button>} />}
        </section>
        <aside className="card attention-card">
          <div className="card-heading"><div><p className="eyebrow">HUMAN IN THE LOOP</p><h2>等待你处理</h2></div><span className="number-chip">{approvals.data.length}</span></div>
          {approvals.error ? <ErrorState message={approvals.error} onRetry={approvals.reload} /> : approvals.loading ? <LoadingState /> : approvals.data.length ? (
            <div className="approval-compact-list">
              {approvals.data.slice(0, 4).map((approval) => (
                <button key={approval.id} className="approval-compact" onClick={() => navigate('/sessions')}>
                  <span className="tool-symbol"><SquareTerminal size={16} /></span>
                  <span><strong>{approval.tool_name ?? '工具调用'}</strong><small>{formatDate(approval.created_at)}</small></span>
                  <ChevronRight size={16} />
                </button>
              ))}
            </div>
          ) : <EmptyState icon={CheckCircle2} title="全部处理完毕" description="当前没有等待审批的高风险操作。" />}
        </aside>
      </div>
      <section className="safety-banner">
        <div className="safety-visual"><ShieldCheck size={23} /></div>
        <div><strong>安全护栏正在工作</strong><p>重复工具调用、无进展循环和无效 API Key 会被自动拦截并记录原因。</p></div>
        <button className="button button-quiet" onClick={() => navigate('/runs')}>查看运行记录</button>
      </section>
    </div>
  )
}

function RunRow({ run, onClick }: { run: Run; onClick?: () => void }) {
  return (
    <button className="run-row" onClick={onClick} disabled={!onClick}>
      <div className="run-avatar"><Bot size={17} /></div>
      <div className="run-main"><strong>{run.title || run.agent_name || `运行 ${run.id.slice(0, 8)}`}</strong><span>{run.phase ? `阶段：${statusText[run.phase] ?? run.phase}` : `${run.step_count ?? run.current_step ?? 0} 步 · ${run.tool_call_count ?? run.tool_calls ?? 0} 次工具调用`}</span></div>
      <StatusBadge status={run.status} />
      <time>{formatDate(run.updated_at || run.finished_at || run.created_at || run.started_at)}</time>
    </button>
  )
}

function AgentsPage() {
  const agents = useApiData<AgentProfile[]>([], () => api.list<AgentProfile>('/api/agents', ['agents']), [])
  const connections = useApiData<Connection[]>([], () => api.list<Connection>('/api/connections', ['connections']), [])
  const tools = useApiData<ToolCatalogItem[]>([], () => api.list<ToolCatalogItem>('/api/tools', ['tools']), [])
  const skills = useApiData<SkillCatalogItem[]>([], () => api.list<SkillCatalogItem>('/api/skills', ['skills']), [])
  const [panelOpen, setPanelOpen] = useState(false)
  const [editing, setEditing] = useState<AgentProfile | null>(null)
  const [selectedToolIds, setSelectedToolIds] = useState<string[]>([])
  const [selectedSkillIds, setSelectedSkillIds] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [deletingId, setDeletingId] = useState('')
  const [formError, setFormError] = useState('')
  const childAgents = agents.data.filter((agent) => !agent.is_default)

  function openAgentPanel(agent?: AgentProfile) {
    if (agent?.is_default) return
    setEditing(agent ?? null)
    setSelectedToolIds(agent?.tool_ids ?? [])
    setSelectedSkillIds(agent?.skill_ids ?? [])
    setFormError('')
    setPanelOpen(true)
  }

  async function saveAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setSaving(true); setFormError('')
    try {
      const payload = {
        name: form.get('name'), description: form.get('description'), system_prompt: form.get('system_prompt'),
        model_connection_id: form.get('connection_id') || null,
        model_id: form.get('model') || null,
        thinking_level: form.get('thinking_level'),
        tool_ids: selectedToolIds,
        skill_ids: selectedSkillIds,
      }
      if (editing) await api.patch(`/api/agents/${editing.id}`, payload)
      else await api.post('/api/agents', payload)
      setPanelOpen(false); setEditing(null); await agents.reload()
    } catch (error) { setFormError(describeError(error)) } finally { setSaving(false) }
  }

  async function deleteAgent(agent: AgentProfile) {
    if (agent.is_default || !window.confirm(`确定删除 Agent“${agent.name}”吗？此操作不可撤销。`)) return
    setDeletingId(agent.id); setFormError('')
    try { await api.delete(`/api/agents/${agent.id}`); await agents.reload() }
    catch (error) { setFormError(describeError(error)) } finally { setDeletingId('') }
  }

  return (
    <div className="page">
      <PageHeader eyebrow="REUSABLE INTELLIGENCE" title="子 Agent" description="创建可复用的专业子 Agent，为不同任务配置角色、系统指令与模型偏好。" action={<button className="button button-primary" onClick={() => openAgentPanel()}><Plus size={16} />创建子 Agent</button>} />
      {formError && !panelOpen && <p className="form-error page-form-error" role="alert">{formError}</p>}
      {agents.error ? <ErrorState message={agents.error} onRetry={agents.reload} /> : agents.loading ? <LoadingState /> : childAgents.length ? (
        <section className="entity-grid agent-grid">
          {childAgents.map((agent) => (
            <article className="entity-card agent-card" key={agent.id}>
              <div className="agent-head"><div className="agent-avatar"><Bot size={22} /></div><div className="agent-card-actions"><StatusBadge status={agent.status || 'idle'} /><button className="icon-button" aria-label={`编辑 ${agent.name}`} title="编辑子 Agent" onClick={() => openAgentPanel(agent)}><Pencil size={15} /></button><button className="icon-button danger-icon" aria-label={`删除 ${agent.name}`} disabled={deletingId === agent.id} title="删除子 Agent" onClick={() => void deleteAgent(agent)}>{deletingId === agent.id ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />}</button></div></div>
              <h2>{agent.name}</h2><p className="agent-role">{agent.role || '通用执行子 Agent'}</p><p>{agent.description || '暂无角色说明'}</p>
              <div className="agent-meta"><span><Sparkles size={14} />{agent.model || agent.model_id || '继承默认模型'}</span><span><Wrench size={14} />{agent.tool_ids?.length ?? 0} 工具 · {agent.skill_ids?.length ?? 0} Skill</span></div>
              <footer><span>{formatDate(agent.created_at)}</span><span>{`思考：${agent.thinking_level || 'auto'}`}</span></footer>
            </article>
          ))}
        </section>
      ) : <EmptyState icon={Bot} title="创建你的第一个子 Agent" description="定义专业角色、系统指令、模型与可用能力；主 Agent 会在复杂、专业或你明确要求时委派匹配的子 Agent。" action={<button className="button button-primary" onClick={() => openAgentPanel()}><Plus size={16} />创建子 Agent</button>} />}
      {panelOpen && <SlidePanel title={editing ? '编辑子 Agent' : '创建子 Agent'} description="配置角色、模型以及允许这个子 Agent 使用的工具和 Skill。主 Agent 会在需要时将任务委派给匹配的子 Agent。" onClose={() => { setPanelOpen(false); setEditing(null) }}>
        <form className="panel-form" onSubmit={saveAgent} key={editing?.id || 'new-agent'}>
          <Field label="名称"><input name="name" required placeholder="例如：代码协作者" autoFocus defaultValue={editing?.name || ''} /></Field>
          <Field label="简介"><input name="description" placeholder="简要描述擅长处理的任务" defaultValue={editing?.description || ''} /></Field>
          <Field label="系统指令"><textarea name="system_prompt" rows={6} placeholder="说明工作原则、输出风格和边界……" defaultValue={editing?.system_prompt || ''} /></Field>
          <div className="form-row"><Field label="模型连接"><select name="connection_id" defaultValue={editing?.model_connection_id || editing?.connection_id || ''}><option value="">使用默认连接</option>{connections.data.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field><Field label="模型 ID"><input name="model" placeholder="例如：deepseek-chat" defaultValue={editing?.model_id || editing?.model || ''} /></Field></div>
          <Field label="思考强度"><select name="thinking_level" defaultValue={editing?.thinking_level || 'auto'}><option value="off">关闭</option><option value="auto">自动</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高</option></select></Field>
          <CapabilityMultiSelect label="工具" items={tools.data.map((item) => ({ ...item, name: item.label || item.name }))} selectedIds={selectedToolIds} loading={tools.loading} error={tools.error} onRetry={() => void tools.reload()} onToggle={(id) => setSelectedToolIds((current) => toggleSelectedId(current, id))} />
          <CapabilityMultiSelect label="Skill" items={skills.data} selectedIds={selectedSkillIds} loading={skills.loading} error={skills.error} onRetry={() => void skills.reload()} onToggle={(id) => setSelectedSkillIds((current) => toggleSelectedId(current, id))} />
          {formError && <p className="form-error" role="alert">{formError}</p>}
          <div className="form-actions"><button type="button" className="button button-secondary" onClick={() => { setPanelOpen(false); setEditing(null) }}>取消</button><button className="button button-primary" disabled={saving}>{saving && <LoaderCircle className="spin" size={15} />}{editing ? '保存修改' : '创建'}</button></div>
        </form>
      </SlidePanel>}
    </div>
  )
}

function SkillsPage() {
  const installed = useApiData<SkillCatalogItem[]>([], () => api.list<SkillCatalogItem>('/api/skills', ['skills']), [])
  const market = useApiData<SkillMarketplaceSearch>({}, () => api.get<SkillMarketplaceSearch>('/api/skills/market/status'), [])
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SkillMarketplaceItem[]>([])
  const [searchedQuery, setSearchedQuery] = useState('')
  const [browseView, setBrowseView] = useState<SkillMarketplaceView>('trending')
  const [browse, setBrowse] = useState<SkillMarketplaceBrowse>({})
  const [browsing, setBrowsing] = useState(false)
  const [browseError, setBrowseError] = useState('')
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState('')
  const [marketMessage, setMarketMessage] = useState('')
  const [importing, setImporting] = useState(false)
  const [installingId, setInstallingId] = useState('')
  const [previews, setPreviews] = useState<Record<string, SkillInstallPreview>>({})
  const [actionError, setActionError] = useState('')
  const [categoryBoards, setCategoryBoards] = useState<SkillMarketplaceCategory[]>([])
  const [boardsLoading, setBoardsLoading] = useState(false)
  const [boardsRefreshing, setBoardsRefreshing] = useState(false)
  const [boardsError, setBoardsError] = useState('')
  const [boardsUpdatedAt, setBoardsUpdatedAt] = useState('')
  const [boardRefreshAfterSeconds, setBoardRefreshAfterSeconds] = useState(30 * 60)
  const browseRequestRef = useRef(0)
  const leaderboardRequestRef = useRef(0)

  const showingSearchResults = searchedQuery !== '' && searchedQuery === query.trim() && !searchError && !searching
  const activeMarketItems = showingSearchResults ? results : (browse.items ?? [])

  const loadBrowse = useCallback(async (view: SkillMarketplaceView, page = 0, append = false) => {
    const requestId = ++browseRequestRef.current
    setBrowsing(true); setBrowseError('')
    try {
      const response = await api.get<SkillMarketplaceBrowse>(`/api/skills/market/browse?view=${encodeURIComponent(view)}&page=${page}&per_page=12`)
      if (requestId === browseRequestRef.current) {
        setBrowse((current) => ({
          ...response,
          items: append ? [...(current.items ?? []), ...(response.items ?? [])] : (response.items ?? []),
        }))
      }
    } catch (error) {
      if (requestId === browseRequestRef.current) setBrowseError(describeError(error))
    } finally {
      if (requestId === browseRequestRef.current) setBrowsing(false)
    }
  }, [])

  useEffect(() => {
    if (market.data.available) void loadBrowse(browseView)
  }, [browseView, loadBrowse, market.data.available])

  const loadCategoryBoards = useCallback(async (manual = false) => {
    const requestId = ++leaderboardRequestRef.current
    if (manual) setBoardsRefreshing(true)
    else setBoardsLoading(true)
    setBoardsError('')
    try {
      let payload: SkillMarketplaceLeaderboards
      try {
        payload = manual
          ? await api.post<SkillMarketplaceLeaderboards>('/api/skills/market/leaderboards/refresh')
          : await api.get<SkillMarketplaceLeaderboards>('/api/skills/market/leaderboards')
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 404) throw error
        const entries = await Promise.all(marketCategoryDefinitions.map(async (category) => {
          const response = await api.post<SkillMarketplaceSearch>('/api/skills/market/search', { query: category.query, limit: 6 })
          return [category.id, response.items ?? []] as const
        }))
        payload = { categories: fallbackMarketCategories(Object.fromEntries(entries)) }
      }
      if (requestId === leaderboardRequestRef.current) {
        setCategoryBoards(normalizeMarketCategories(payload))
        setBoardsUpdatedAt(payload.updated_at || payload.refreshed_at || new Date().toISOString())
        setBoardRefreshAfterSeconds(payload.refresh_after_seconds || payload.refresh_interval_seconds || payload.ttl_seconds || 30 * 60)
      }
    } catch (error) {
      if (requestId === leaderboardRequestRef.current) setBoardsError(describeError(error))
    } finally {
      if (requestId === leaderboardRequestRef.current) {
        setBoardsLoading(false)
        setBoardsRefreshing(false)
      }
    }
  }, [])

  useEffect(() => {
    if (!market.data.available) return
    void loadCategoryBoards()
  }, [loadCategoryBoards, market.data.available])

  useEffect(() => {
    if (!market.data.available) return
    const timer = window.setInterval(() => void loadCategoryBoards(), leaderboardRefreshDelayMs(boardRefreshAfterSeconds))
    return () => window.clearInterval(timer)
  }, [boardRefreshAfterSeconds, loadCategoryBoards, market.data.available])

  async function importLocalSkill() {
    if (importing) return
    setImporting(true); setActionError('')
    try {
      const selection = await api.post<FolderSelection>('/api/system/select-folder')
      if (!selection.path) return
      await api.post('/api/skills/import', { source_path: selection.path })
      await installed.reload()
    } catch (error) { setActionError(describeError(error)) } finally { setImporting(false) }
  }

  async function searchMarketQuery(normalizedQuery: string) {
    if (normalizedQuery.length < 2 || searching) return
    setSearching(true); setSearchError(''); setMarketMessage('')
    try {
      const response = await api.post<SkillMarketplaceSearch>('/api/skills/market/search', { query: normalizedQuery, limit: 20 })
      setResults(response.items ?? [])
      setSearchedQuery(normalizedQuery)
      setMarketMessage(response.message ?? '')
    } catch (error) {
      setResults([])
      setSearchedQuery('')
      setSearchError(describeError(error))
    } finally { setSearching(false) }
  }

  function searchMarket(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void searchMarketQuery(query.trim())
  }

  function chooseBrowseView(view: SkillMarketplaceView) {
    setResults([]); setQuery(''); setSearchedQuery(''); setSearchError(''); setMarketMessage('')
    if (view !== browseView) setBrowseView(view)
    else void loadBrowse(view)
  }

  async function previewMarketSkill(item: SkillMarketplaceItem) {
    if (installingId) return
    setInstallingId(item.id); setActionError('')
    try {
      const preview = await api.post<SkillInstallPreview>('/api/skills/market/install', { market_id: item.id, confirm: false })
      setPreviews((current) => ({ ...current, [item.id]: preview }))
    } catch (error) { setActionError(describeError(error)) } finally { setInstallingId('') }
  }

  async function confirmMarketSkill(item: SkillMarketplaceItem) {
    if (installingId || !previews[item.id]) return
    setInstallingId(item.id); setActionError('')
    try {
      await api.post('/api/skills/market/install', { market_id: item.id, confirm: true })
      setPreviews((current) => {
        const next = { ...current }
        delete next[item.id]
        return next
      })
      await installed.reload()
    } catch (error) { setActionError(describeError(error)) } finally { setInstallingId('') }
  }

  function showCategorySearch(category: SkillMarketplaceCategory) {
    const categoryQuery = category.query || category.label || ''
    setQuery(categoryQuery)
    void searchMarketQuery(categoryQuery)
    window.requestAnimationFrame(() => document.querySelector<HTMLInputElement>('.skill-search input')?.focus())
  }

  return <div className="page skills-page">
    <PageHeader eyebrow="CAPABILITY LIBRARY" title="技能库" description="管理已安装的 Skill，或从本地文件夹与在线市场添加新能力。" action={<button className="button button-primary" disabled={importing} onClick={() => void importLocalSkill()}>{importing ? <LoaderCircle className="spin" size={15} /> : <Upload size={15} />}导入本地 Skill</button>} />
    {actionError && <p className="form-error page-form-error" role="alert">{actionError}</p>}
    <section className="skills-section">
      <div className="skills-section-heading"><div><h2>已安装</h2><p>这里只显示后端实际返回的 Skill。</p></div><span>{installed.data.length}</span></div>
      {installed.error ? <ErrorState message={installed.error} onRetry={installed.reload} /> : installed.loading ? <LoadingState /> : installed.data.length ? <div className="skill-card-grid">
        {installed.data.map((skill) => <article className="skill-card" key={skill.id}>
          <div className="skill-card-icon"><BookOpen size={18} /></div>
          <div><h3>{skill.name}</h3><p>{skill.description || '暂无说明'}</p></div>
          <dl><div><dt>版本</dt><dd>{skill.version || '未标注'}</dd></div><div><dt>来源</dt><dd title={skill.source_url || skill.source}>{skill.source || 'local'}</dd></div></dl>
        </article>)}
      </div> : <EmptyState icon={BookOpen} title="还没有安装 Skill" description="选择本地 Skill 文件夹导入，或在下方搜索在线市场。" />}
    </section>
    <section className="skills-section skill-market-section">
      <div className="skills-section-heading"><div><h2>在线市场</h2><p>{market.data.provider ? `来源：${market.data.provider} · 仅展示安装量，不代表活跃用户数。` : '搜索可下载的 Skill。'}</p></div></div>
      {market.error ? <ErrorState message={market.error} onRetry={market.reload} /> : market.loading ? <LoadingState label="正在检查市场服务" /> : market.data.available === false ? <ErrorState message={market.data.message || '在线市场当前不可用。'} onRetry={market.reload} /> : <>
        <section className="market-leaderboards" aria-label="按类型浏览热门 Skill">
          <header className="market-leaderboards-header">
            <div><strong>热门分类榜单</strong><small>{boardsUpdatedAt ? `更新于 ${formatDate(boardsUpdatedAt)} · 定期自动刷新` : '每类展示安装量最高的 6 个 Skill · 定期自动刷新'}</small></div>
            <button type="button" className="button button-secondary market-refresh-button" disabled={boardsLoading || boardsRefreshing} onClick={() => void loadCategoryBoards(true)}>{boardsLoading || boardsRefreshing ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}刷新榜单</button>
          </header>
          {boardsError ? <ErrorState message={boardsError} onRetry={() => void loadCategoryBoards(true)} /> : boardsLoading && !categoryBoards.length ? <LoadingState label="正在整理分类榜单" /> : <div className="market-category-grid">
            {categoryBoards.map((category) => <section className="market-category-card" key={category.id}>
              <header><div><h3>{category.label || category.id}</h3><p>{category.description || '热门可下载 Skill'}</p></div><button type="button" className="text-button" onClick={() => showCategorySearch(category)}>查看全部<ChevronRight size={13} /></button></header>
              {category.items?.length ? <ol>{category.items.slice(0, 6).map((item, index) => {
                const preview = previews[item.id]
                return <Fragment key={item.id}>
                  <li>
                    <span className="market-category-rank">{index + 1}</span><div><strong title={item.name}>{item.name}</strong><small title={item.slug || item.source || item.id}>{item.slug || item.source || item.id}</small></div><span className="market-category-installs">{typeof item.installs === 'number' ? `${item.installs.toLocaleString()} 次` : '—'}</span><button type="button" className="market-category-download" aria-label={`预览并下载 ${item.name}`} disabled={!!installingId} onClick={() => void previewMarketSkill(item)}>{installingId === item.id ? <LoaderCircle className="spin" size={13} /> : <Download size={13} />}</button>
                  </li>
                  {preview && <li className="market-category-preview"><span>已预览 {preview.files?.length ?? 0} 个文件</span><button type="button" className="button button-primary" disabled={!!installingId} onClick={() => void confirmMarketSkill(item)}>{installingId === item.id ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />}确认导入</button></li>}
                </Fragment>
              })}</ol> : <p className="market-category-empty">暂无可展示的 Skill，刷新后再试。</p>}
            </section>)}
          </div>}
        </section>
        <form className="skill-search" onSubmit={searchMarket}>
          <Search size={16} /><input aria-label="搜索在线 Skill" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入至少 2 个字符搜索…" /><button className="button button-secondary" disabled={query.trim().length < 2 || searching}>{searching ? <LoaderCircle className="spin" size={14} /> : <Search size={14} />}搜索</button>
        </form>
        {searchError && <ErrorState message={searchError} onRetry={() => { const form = document.querySelector<HTMLFormElement>('.skill-search'); form?.requestSubmit() }} />}
        {!searchError && <div className="market-browser">
          <div className="market-browser-header"><strong>{showingSearchResults ? `搜索结果 · ${activeMarketItems.length}` : browse.total !== undefined && browse.total !== null ? `${browse.total.toLocaleString()} 个可浏览 Skill` : '发现 Skill'}</strong>{showingSearchResults && <button type="button" className="text-button" onClick={() => { setResults([]); setQuery(''); setSearchedQuery(''); void loadBrowse(browseView) }}>返回榜单</button>}</div>
          <div className="market-view-tabs" role="tablist" aria-label="Skill 市场榜单">
            {([{ id: 'trending', label: '趋势' }, { id: 'hot', label: '热度' }, { id: 'all-time', label: '热门' }, { id: 'curated', label: '官方精选' }] as Array<{ id: SkillMarketplaceView; label: string }>).map((tab) => <button key={tab.id} type="button" role="tab" aria-selected={!showingSearchResults && browseView === tab.id} className={!showingSearchResults && browseView === tab.id ? 'active' : ''} onClick={() => chooseBrowseView(tab.id)}>{tab.label}</button>)}
          </div>
        </div>}
        {marketMessage && <p className="market-message">{marketMessage}</p>}
        {browseError && !showingSearchResults && <ErrorState message={browseError} onRetry={() => void loadBrowse(browseView)} />}
        {(searching || (browsing && !activeMarketItems.length)) && <LoadingState label={searching ? '正在搜索 Skill' : '正在读取榜单'} />}
        {!searching && !browseError && activeMarketItems.length ? <div className="market-results">{activeMarketItems.map((item) => {
          const preview = previews[item.id]
          return <article key={item.id} className={preview ? 'has-preview' : ''}>
            <div><strong>{item.name}</strong><small>{item.slug || item.source || item.id}{item.is_duplicate ? ' · 重复来源' : ''}</small>{item.is_official && <em>官方精选{item.official_owner ? ` · ${item.official_owner}` : ''}</em>}</div>
            <span className="market-metrics">{typeof item.installs === 'number' && <b>{item.installs.toLocaleString()} 次安装</b>}{browseView === 'hot' && typeof item.change === 'number' && <small className={item.change > 0 ? 'positive' : ''}>{item.change >= 0 ? '+' : ''}{item.change} / 小时</small>}</span>
            <button className="button button-secondary" disabled={!!installingId} onClick={() => void previewMarketSkill(item)}>{installingId === item.id && !preview ? <LoaderCircle className="spin" size={14} /> : <Download size={14} />}{preview ? '重新预览' : '下载'}</button>
            {preview && <div className="skill-preview">
              <p><strong>来源</strong><span title={preview.source_url}>{preview.source_url}</span></p>
              <p><strong>候选目录</strong><span>{preview.candidates?.length ? preview.candidates.join('、') : '默认目录'}</span></p>
              <p><strong>文件</strong><span>{preview.files?.length ?? 0} 个</span></p>
              <button className="button button-primary" disabled={!!installingId} onClick={() => void confirmMarketSkill(item)}>{installingId === item.id ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}确认导入</button>
            </div>}
          </article>
        })}</div> : !searching && !browsing && !searchError && !browseError && <p className="market-empty">{showingSearchResults ? `未找到与“${searchedQuery}”匹配的 Skill。` : '这里会显示真实的市场结果。下载前会先展示文件清单，确认后才会导入。'}</p>}
        {!showingSearchResults && !browseError && browse.has_more && <div className="market-load-more"><button type="button" className="button button-secondary" disabled={browsing} onClick={() => void loadBrowse(browseView, (browse.page ?? 0) + 1, true)}>{browsing ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}加载更多</button></div>}
      </>}
    </section>
  </div>
}

function SessionsPage() {
  const sessions = useApiData<Session[]>([], () => api.list<Session>('/api/sessions', ['sessions']), [])
  const agents = useApiData<AgentProfile[]>([], () => api.list<AgentProfile>('/api/agents', ['agents']), [])
  const workspaces = useApiData<Workspace[]>([], () => api.list<Workspace>('/api/workspaces', ['workspaces']), [])
  const connections = useApiData<Connection[]>([], () => api.list<Connection>('/api/connections', ['connections']), [])
  const skills = useApiData<SkillCatalogItem[]>([], () => api.list<SkillCatalogItem>('/api/skills', ['skills']), [])
  const [activeId, setActiveId] = useState('')
  const [composer, setComposer] = useState('')
  const [sending, setSending] = useState(false)
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [settingsMenuOpen, setSettingsMenuOpen] = useState(false)
  const [settingsSubmenu, setSettingsSubmenu] = useState<'model' | 'thinking' | null>(null)
  const [addMenuOpen, setAddMenuOpen] = useState(false)
  const [skillSubmenuOpen, setSkillSubmenuOpen] = useState(false)
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
  const [completedThoughtsByRun, setCompletedThoughtsByRun] = useState<Record<string, ThoughtTimelineState>>({})
  const [childPanelOpen, setChildPanelOpen] = useState(false)
  const [selectedChildTaskId, setSelectedChildTaskId] = useState('')
  const [liveRun, setLiveRun] = useState<LiveRunState>(emptyLiveRun)
  const eventSourceRef = useRef<EventSource | null>(null)
  const fallbackTimerRef = useRef<number | null>(null)
  const streamReconnectTimerRef = useRef<number | null>(null)
  const streamErrorCountRef = useRef(0)
  const streamRunIdRef = useRef('')
  const seenStreamEventsRef = useRef<{ runId: string; eventIds: Set<string> }>({ runId: '', eventIds: new Set() })
  const runStartMessageCountRef = useRef(0)
  const messagesRef = useRef<HTMLDivElement>(null)
  const activeIdRef = useRef('')
  const stickToBottomRef = useRef(true)
  const terminalSyncVersionRef = useRef(0)
  const pendingDraftRunRef = useRef<{ sessionId: string; runId: string } | null>(null)
  const draftIdempotencyKeyRef = useRef('')
  const draftVersionRef = useRef(0)
  const sendingRef = useRef(false)
  const loadedThoughtRunIdsRef = useRef(new Set<string>())
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
      }
      if (permissionMenuOpen && !permissionMenuRef.current?.contains(target)) setPermissionMenuOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setAddMenuOpen(false)
      setSkillSubmenuOpen(false)
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
    setAddMenuOpen(false); setSkillSubmenuOpen(false); setPermissionMenuOpen(false)
    setChildPanelOpen(false); setSelectedChildTaskId('')
  }, [activeId])

  const messages = useApiData<OwnedSessionMessages>(
    { ownerSessionId: '', items: [] },
    async () => activeId
      ? { ownerSessionId: activeId, items: await api.list<Message>(`/api/sessions/${activeId}/messages`, ['messages']) }
      : { ownerSessionId: '', items: [] },
    [activeId],
  )
  const runs = useApiData<Run[]>([], () => activeId ? api.list<Run>(`/api/runs?session_id=${encodeURIComponent(activeId)}`, ['runs']) : Promise.resolve([]), [activeId])
  const childTasks = useApiData<DelegatedTask[]>([], async () => {
    if (!activeId) return []
    return api.list<DelegatedTask>(`/api/sessions/${encodeURIComponent(activeId)}/delegations`, ['delegations'])
  }, [activeId])
  const context = useApiData<SessionContext | null>(null, () => activeId ? api.get<SessionContext>(`/api/sessions/${activeId}/context`) : Promise.resolve(null), [activeId])
  const activeSession = sessions.data.find((item) => stringId(item.id) === activeId)
  const activeAgent = agents.data.find((item) => item.id === activeSession?.agent_id)
  const sessionRuns = runs.data.filter((item) => !item.session_id || item.session_id === activeId)
  const awaitingApprovalRunIds = sessionRuns
    .filter((item) => item.status === 'awaiting_approval')
    .map((item) => item.id)
  const approvalRunIdsKey = awaitingApprovalRunIds.join(',')
  // A child awaiting approval takes precedence over its parent so the card is
  // actionable in this very conversation instead of hidden behind a parent
  // run that has already stopped for the child.
  const activeRun = sessionRuns.find((item) => item.status === 'awaiting_approval')
    ?? sessionRuns.find((item) => activeRunStatuses.has(item.status || ''))
    ?? sessionRuns[0]
  const activeRunId = activeRun?.id || ''
  // Legacy versions wrote raw child responses into the chat transcript. Hide
  // those rows too; the source of truth is now the child side panel.
  const visibleMessages = visibleSessionItems(messages.data.ownerSessionId, activeId, messages.data.items)
    .filter((message) => message.metadata?.delegated_child !== true)
  const sessionNavigation = buildSessionNavigation(workspaces.data, sessions.data)
  const activeChildTask = childTasks.data.find((task) => task.id === selectedChildTaskId) ?? childTasks.data[0]
  const childTaskRunId = stringId(activeChildTask?.child_run_id) || stringId(activeChildTask?.result?.child_run_id)
  const childTaskRun = childTaskRunId ? sessionRuns.find((run) => run.id === childTaskRunId) : undefined
  const childTaskEvents = useApiData<RunEvent[]>([], () => childTaskRunId
    ? api.list<RunEvent>(`/api/runs/${encodeURIComponent(childTaskRunId)}/events`, ['events'])
    : Promise.resolve([]), [childTaskRunId])
  const hasChildActivity = childTasks.data.length > 0 || liveRun.phase.includes('子 Agent')
  const approvals = useApiData<Approval[]>([], async () => {
    const runIds = approvalRunIdsKey ? approvalRunIdsKey.split(',').filter(Boolean) : []
    if (!runIds.length) return []
    const groups = await Promise.all(runIds.map((runId) => api.list<Approval>(
      `/api/approvals?run_id=${encodeURIComponent(runId)}&status=pending`,
      ['approvals'],
    )))
    return groups.flat()
  }, [approvalRunIdsKey])
  const visibleApprovals = approvals.data.filter((approval) => awaitingApprovalRunIds.includes(stringId(approval.run_id)))
  const refreshMessages = messages.refresh
  const refreshRuns = runs.refresh
  const refreshChildTasks = childTasks.refresh
  const refreshContext = context.refresh
  const setMessagesState = messages.setState
  const setApprovalsState = approvals.setState
  const settingsLocked = activeRunStatuses.has(activeRun?.status || '') || !['idle', 'terminal'].includes(liveRun.status)

  useEffect(() => {
    if (liveRun.status !== 'terminal' || !liveRun.runId || !hasVisibleCompletedThought(liveRun.thought)) return
    setCompletedThoughtsByRun((current) => current[liveRun.runId] === liveRun.thought ? current : { ...current, [liveRun.runId]: liveRun.thought })
  }, [liveRun.runId, liveRun.status, liveRun.thought])

  useEffect(() => {
    setCompletedThoughtsByRun({})
    loadedThoughtRunIdsRef.current = new Set()
  }, [activeId])

  useEffect(() => {
    if (!childTasks.data.length) {
      setSelectedChildTaskId('')
      return
    }
    if (!childTasks.data.some((task) => task.id === selectedChildTaskId)) {
      setSelectedChildTaskId(childTasks.data[0].id)
    }
  }, [childTasks.data, selectedChildTaskId])

  useEffect(() => {
    if (!activeId) return
    let cancelled = false
    const finishedRuns = runs.data.filter((run) => (!run.session_id || run.session_id === activeId) && isTerminalRunStatus(run.status))
    for (const run of finishedRuns) {
      if (loadedThoughtRunIdsRef.current.has(run.id)) continue
      loadedThoughtRunIdsRef.current.add(run.id)
      void api.list<RunEvent>(`/api/runs/${encodeURIComponent(run.id)}/events`, ['events']).then((events) => {
        if (cancelled || activeIdRef.current !== activeId) return
        const timeline = timelineFromRunEvents(events.map((event) => ({ ...event, type: event.type || event.event_type || '' })))
        if (hasVisibleCompletedThought(timeline)) setCompletedThoughtsByRun((current) => ({ ...current, [run.id]: timeline }))
      }).catch(() => { loadedThoughtRunIdsRef.current.delete(run.id) })
    }
    return () => { cancelled = true }
  }, [activeId, runs.data])
  useEffect(() => {
    if (settingsLocked || sending) {
      setSettingsMenuOpen(false)
      setSettingsSubmenu(null)
      setAddMenuOpen(false)
      setSkillSubmenuOpen(false)
      setPermissionMenuOpen(false)
    }
  }, [sending, settingsLocked])

  useEffect(() => {
    setMessagesState({ data: { ownerSessionId: activeId, items: [] }, loading: true, error: '' })
  }, [activeId, setMessagesState])

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
    setComposer('')
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
      setComposer('')
    }
    setActionError('')
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

  async function addProjectFromFolder() {
    if (addingProject) return
    setAddingProject(true); setProjectError('')
    try {
      const selection = await api.post<FolderSelection>('/api/system/select-folder')
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
      const selection = await api.post<FolderSelection>('/api/system/select-folder')
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
  }, [])

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
      setApprovalsState({ data, loading: false, error: '' })
      return data
    } catch {
      return undefined
    }
  }, [setApprovalsState])

  const syncTerminalRun = useCallback(async (runId: string, event?: RunStreamEvent, sessionId = activeIdRef.current) => {
    const syncVersion = ++terminalSyncVersionRef.current
    closeRunTransport()
    setLiveRun((previous) => ({
      ...previous,
      runId,
      phase: event ? runStreamPhase(event) : previous.phase,
      status: 'terminal',
      error: event?.error ? String(event.error) : previous.error,
      thought: event ? updateThoughtTimeline(previous.thought, event) : previous.thought,
    }))

    let refreshedMessages: OwnedSessionMessages | undefined
    for (const delay of [220, 520, 900]) {
      if (delay > 220) await new Promise((resolve) => window.setTimeout(resolve, delay))
      if (syncVersion !== terminalSyncVersionRef.current || activeIdRef.current !== sessionId) return
      refreshedMessages = await refreshMessages()
      const assistantCount = refreshedMessages?.ownerSessionId === sessionId
        ? refreshedMessages.items.filter((message) => message.role === 'assistant').length
        : 0
      if (assistantCount > runStartMessageCountRef.current) break
    }
    if (syncVersion !== terminalSyncVersionRef.current || activeIdRef.current !== sessionId) return

    await Promise.all([refreshRuns(), refreshContext(), refreshChildTasks(), refreshApprovalsForSession(sessionId)])
    if (syncVersion !== terminalSyncVersionRef.current || activeIdRef.current !== sessionId) return

    const assistantCount = refreshedMessages?.ownerSessionId === sessionId
      ? refreshedMessages.items.filter((message) => message.role === 'assistant').length
      : 0
    const hasPersistedReply = assistantCount > runStartMessageCountRef.current
    const hasDraft = Boolean(liveRunRef.current.runId === runId && liveRunRef.current.draft)
    if (event?.error) setActionError(`运行失败：${String(event.error)}`)
    if (hasPersistedReply || !hasDraft) {
      setLiveRun(emptyLiveRun())
    }
  }, [closeRunTransport, refreshApprovalsForSession, refreshChildTasks, refreshContext, refreshMessages, refreshRuns])

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
          run = latestRuns?.find((item) => item.id === runId)
        }
        if (!run || streamRunIdRef.current !== runId || activeIdRef.current !== sessionId) return
        const status = run.status || ''
        setLiveRun((previous) => ({
          ...previous,
          runId,
          phase: runStatusPhase(status),
          status: status === 'awaiting_approval' ? 'awaiting_approval' : isTerminalRunStatus(status) ? 'terminal' : 'fallback',
          error: '',
        }))
        void refreshRuns()
        if (status === 'awaiting_approval') void refreshApprovalsForSession(sessionId)
        if (isTerminalRunStatus(status)) {
          await syncTerminalRun(runId, { type: 'run_state', status, terminal: true, error: status === 'failed' ? run.stop_reason : undefined, reason: run.stop_reason }, sessionId)
        }
      } finally {
        polling = false
      }
    }

    void poll()
    fallbackTimerRef.current = window.setInterval(() => void poll(), 4000)
  }, [refreshApprovalsForSession, refreshRuns, syncTerminalRun])

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
        draft: appendAssistantDelta(previous.draft, parsed),
        status: terminal ? 'terminal' : waitingApproval ? 'awaiting_approval' : 'live',
        error: parsed.error ? String(parsed.error) : previous.error,
        thought: updateThoughtTimeline(previous.thought, parsed),
      }))
      if (waitingApproval) void refreshApprovalsForSession(sessionId)
      if (delegatedChildEvent) {
        void refreshRuns()
        void refreshChildTasks()
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
  }, [refreshApprovalsForSession, refreshChildTasks, refreshRuns, startRunFallback, syncTerminalRun])

  useEffect(() => {
    closeRunTransport()
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
    runStartMessageCountRef.current = visibleMessages.filter((message) => message.role === 'assistant').length
    startRunStream(pending.runId, pending.sessionId)
    void refreshMessages()
    void refreshRuns()
    void refreshContext()
  }, [activeId, refreshContext, refreshMessages, refreshRuns, startRunStream, visibleMessages])

  useEffect(() => {
    if (!activeId || messages.loading || !activeRun?.id || !activeRunStatuses.has(activeRun.status || '') || liveRun.status === 'terminal') return
    if (streamRunIdRef.current === activeRun.id) return
    runStartMessageCountRef.current = visibleMessages.filter((message) => message.role === 'assistant').length
    startRunStream(activeRun.id, activeId)
  }, [activeId, activeRun?.id, activeRun?.status, liveRun.status, messages.loading, startRunStream, visibleMessages])

  useEffect(() => {
    if (!stickToBottomRef.current) return
    const frame = window.requestAnimationFrame(() => {
      const element = messagesRef.current
      if (element) element.scrollTop = element.scrollHeight
    })
    return () => window.cancelAnimationFrame(frame)
  }, [liveRun.draft, liveRun.phase, visibleApprovals.length, visibleMessages.length])

  async function sendMessage(event: FormEvent) {
    event.preventDefault()
    const content = composer.trim()
    if (sendingRef.current || (!activeId && !draftActive) || !content || settingsSaving || capabilitySaving || settingsLocked) return
    sendingRef.current = true
    setSending(true); setActionError('')
    stickToBottomRef.current = true
    runStartMessageCountRef.current = visibleMessages.filter((message) => message.role === 'assistant').length
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
        pendingDraftRunRef.current = { sessionId, runId }
        setComposer('')
        clearDraftState()
        setActiveId(sessionId)
        void sessions.refresh()
        if (draftRootPath || launched.workspace) void workspaces.refresh()
        return
      }

      const targetSessionId = activeId
      const launched = await api.post<Run>(`/api/sessions/${targetSessionId}/run`, { content })
      setComposer('')
      void messages.refresh()
      void runs.refresh()
      void context.refresh()
      startRunStream(launched.id, targetSessionId)
    } catch (error) {
      setLiveRun(emptyLiveRun())
      setActionError(describeError(error))
    } finally {
      sendingRef.current = false
      setSending(false)
    }
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

  async function updateSessionSettings(payload: { model_connection_id?: string | null; model_id?: string | null; thinking_level?: ThinkingLevel | null }) {
    if (settingsLocked || sending) return
    if (draftActive) {
      setDraftSettings((current) => ({
        ...current,
        model_connection_id: payload.model_connection_id === undefined ? current.model_connection_id : payload.model_connection_id,
        model_id: payload.model_id === undefined ? current.model_id : payload.model_id,
        thinking_level: payload.thinking_level ?? current.thinking_level,
      }))
      return
    }
    if (!activeId) return
    setSettingsSaving(true); setActionError('')
    try { await api.patch(`/api/sessions/${activeId}`, payload); await sessions.reload() }
    catch (error) { setActionError(describeError(error)) } finally { setSettingsSaving(false) }
  }

  async function updateSessionCapabilities(payload: { skill_ids?: string[]; permission_mode?: PermissionMode }) {
    if (settingsLocked || sending || capabilitySaving) return
    if (draftActive) {
      setDraftSettings((current) => ({
        ...current,
        skill_ids: payload.skill_ids ?? current.skill_ids,
        permission_mode: payload.permission_mode ?? current.permission_mode,
      }))
      return
    }
    if (!activeId) return
    setCapabilitySaving(true); setActionError('')
    try { await api.patch(`/api/sessions/${activeId}`, payload); await sessions.reload() }
    catch (error) { setActionError(describeError(error)) } finally { setCapabilitySaving(false) }
  }

  const selectedSessionSkillIds = draftActive ? draftSettings.skill_ids : activeSession?.skill_ids ?? []
  const selectedPermissionMode: PermissionMode = draftActive ? draftSettings.permission_mode : activeSession?.permission_mode ?? 'smart'

  function toggleSessionSkill(skillId: string) {
    void updateSessionCapabilities({ skill_ids: toggleSelectedId(selectedSessionSkillIds, skillId) })
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
  } : undefined)
  const effectiveSettings = resolveEffectiveModelSettings(connections.data, settingsSession, activeAgent)
  const effectiveConnection = effectiveSettings.connection
  const effectiveModel = effectiveSettings.model
  const automaticSessionConnection = effectiveSettings.automaticConnection
  const automaticModel = effectiveSettings.automaticModel
  const effectiveThinking = resolveEffectiveThinking(settingsSession?.thinking_level, activeAgent?.thinking_level, effectiveConnection?.thinking_level)
  const modelOptions = connections.data.filter((connection) => connection.enabled !== false).flatMap((connection) => {
    return availableConnectionModels(connection).map((model) => ({ value: `${connection.id}::${model}`, label: model, connection: connection.name }))
  })
  const selectedModelValue = effectiveSettings.selectedValue
  const selectedThinkingValue = settingsSession?.thinking_level && settingsSession.thinking_level !== 'auto' ? settingsSession.thinking_level : 'auto'
  const modelButtonLabel = shortModelLabel(effectiveModel)
  const thinkingOptions: Array<{ value: ThinkingLevel; label: string; hint?: string }> = [
    { value: 'auto', label: '自动 / 继承', hint: `当前：${thinkingLevelLabels[effectiveThinking]}` },
    { value: 'off', label: '关闭' },
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
      <div className="chat-shell">
          <aside className="session-list project-session-sidebar">
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
                  <div className="project-row-wrap">
                    <button type="button" className="project-row" aria-expanded={expanded} title={`${projectSessions.length} 个对话\n${path}`} onClick={() => toggleProject(workspace.id)}>
                      <ChevronRight className="project-chevron" size={13} /><Folder size={15} /><span>{workspace.name}</span>
                    </button>
                    <div className="project-tooltip" role="tooltip"><strong>{projectSessions.length} 个对话</strong><span>{path}</span></div>
                  </div>
                  {expanded && <div className="project-children">
                    {projectSessions.length ? projectSessions.map((session) => <button type="button" key={session.id} className={`tree-session-item ${activeId === stringId(session.id) && !draftActive ? 'active' : ''}`} disabled={draftActive && sending} onClick={() => openExistingSession(stringId(session.id))}><MessageSquare size={13} /><span>{session.title || '未命名对话'}</span></button>) : <p className="tree-empty">暂无对话</p>}
                  </div>}
                </div>
              })}
              {!sessionNavigation.projects.length && !workspaces.loading && <p className="tree-empty tree-empty-projects">点击右上角 + 添加项目</p>}
            </section>

            <section className="task-tree-section">
              <header className="sidebar-section-heading"><strong>任务</strong></header>
              {sessionNavigation.tasks.map((session) => <button type="button" key={session.id} className={`tree-session-item task-session-item ${activeId === stringId(session.id) && !draftActive ? 'active' : ''}`} disabled={draftActive && sending} onClick={() => openExistingSession(stringId(session.id))}><MessageSquare size={13} /><span>{session.title || '未命名对话'}</span></button>)}
              {!draftActive && !sessionNavigation.tasks.length && !sessions.loading && <p className="tree-empty">暂无一次性任务</p>}
            </section>

            {sessions.error && <div className="session-list-error"><AlertCircle size={14} /><span>{sessions.error}</span><button type="button" onClick={() => void sessions.reload()}>重试</button></div>}
            {!!dependencyErrors.length && <div className="session-list-error"><AlertCircle size={14} /><span>{dependencyErrors.join('；')}</span><button type="button" onClick={() => { void Promise.all([agents.reload(), workspaces.reload(), connections.reload()]) }}>重试</button></div>}
            {sessions.loading && !sessions.data.length && <LoadingState />}
          </aside>
          <section className={`conversation ${childPanelOpen ? 'with-child-panel' : ''}`}>
            {activeSession || draftActive ? <>
              {hasChildActivity && <div className="child-panel-toggle-row">
                <button type="button" className="child-panel-toggle" aria-label={childPanelOpen ? '收起子 Agent 面板' : '打开子 Agent 面板'} title={childPanelOpen ? '收起子 Agent 面板' : '打开子 Agent 面板'} aria-expanded={childPanelOpen} onClick={() => setChildPanelOpen((open) => !open)}>
                  {childPanelOpen ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}
                </button>
              </div>}
              <div
                className="messages"
                ref={messagesRef}
                aria-live="polite"
                onScroll={(event) => {
                  const element = event.currentTarget
                  stickToBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80
                }}
              >
                {draftActive ? liveRun.status === 'idle' && <EmptyState icon={MessageSquare} title="开始一次新任务" description="直接描述目标；需要处理本地文件时，可以在输入框中选择一个项目文件夹。" /> : messages.error && !visibleMessages.length ? <ErrorState message={messages.error} onRetry={messages.reload} /> : messages.loading && !visibleMessages.length ? <LoadingState /> : visibleMessages.length ? visibleMessages.map((message) => {
                  const messageRunId = message.role === 'assistant' ? stringId(message.metadata?.run_id) : ''
                  const completedThought = messageRunId ? completedThoughtsByRun[messageRunId] : undefined
                  return <Fragment key={message.id}>{completedThought && <CompletedThoughtTimeline runId={messageRunId} timeline={completedThought} />}<MessageBubble message={message} /></Fragment>
                }) : liveRun.status === 'idle' ? <EmptyState icon={MessageSquare} title="从一条清晰的任务开始" description="描述目标、约束和期望产物，Agent 会先理解上下文再行动。" /> : null}
                {!draftActive && messages.error && !!visibleMessages.length && <p className="inline-error" role="alert">消息同步失败：{messages.error}</p>}
                {liveRun.status !== 'idle' && !completedThoughtsByRun[liveRun.runId] && <LiveAssistantMessage liveRun={liveRun} />}
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
                <textarea aria-label="给 Agent 发送消息" value={composer} disabled={draftActive && sending} onChange={(event) => setComposer(event.target.value)} placeholder={draftActive ? '描述你想完成的任务……' : '告诉 PGAgent 你想完成什么……'} rows={2} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} />
                <div className="composer-toolbar">
                  <div className="composer-left-actions">
                    <div className="session-capability-picker" ref={addMenuRef}>
                      <button type="button" className="composer-tool-button composer-plus-button" aria-label="添加能力" aria-haspopup="menu" aria-expanded={addMenuOpen} disabled={settingsLocked || sending || capabilitySaving} onClick={() => { setAddMenuOpen((open) => !open); setSkillSubmenuOpen(false); setPermissionMenuOpen(false) }}>
                        {capabilitySaving ? <LoaderCircle className="spin" size={14} /> : <Plus size={15} />}
                        {!!selectedSessionSkillIds.length && <b>{selectedSessionSkillIds.length}</b>}
                      </button>
                      {addMenuOpen && <div className="capability-popover capability-level-two" role="menu" aria-label="添加能力">
                        <button type="button" className={skillSubmenuOpen ? 'active' : ''} role="menuitem" aria-haspopup="menu" aria-expanded={skillSubmenuOpen} onMouseEnter={() => setSkillSubmenuOpen(true)} onClick={() => setSkillSubmenuOpen((open) => !open)}><BookOpen size={14} /><span>Skill</span><small>{selectedSessionSkillIds.length ? `已选 ${selectedSessionSkillIds.length}` : '未选择'}</small><ChevronRight size={13} /></button>
                        {skillSubmenuOpen && <div className="capability-popover capability-level-three" role="menu" aria-label="选择 Skill">
                          <p>可用 Skill</p>
                          {skills.error ? <div className="capability-menu-state error"><AlertCircle size={13} /><span>{skills.error}</span><button type="button" onClick={() => void skills.reload()}>重试</button></div>
                            : skills.loading ? <div className="capability-menu-state"><LoaderCircle className="spin" size={13} />正在读取…</div>
                              : skills.data.length ? skills.data.map((skill) => {
                                const selected = selectedSessionSkillIds.includes(skill.id)
                                return <button key={skill.id} type="button" role="menuitemcheckbox" aria-checked={selected} className={selected ? 'selected' : ''} disabled={skill.enabled === false || capabilitySaving} onClick={() => toggleSessionSkill(skill.id)}><span><strong>{skill.name}</strong><small>{skill.description || skill.slug || 'Skill'}</small></span>{selected && <Check size={14} />}</button>
                              }) : <div className="capability-menu-state">技能库中暂无 Skill。</div>}
                        </div>}
                      </div>}
                    </div>
                    <div className="session-capability-picker permission-picker" ref={permissionMenuRef}>
                      <button type="button" className="composer-tool-button permission-trigger" aria-label={`权限模式：${permissionLabel(selectedPermissionMode)}`} aria-haspopup="menu" aria-expanded={permissionMenuOpen} disabled={settingsLocked || sending || capabilitySaving} onClick={() => { setPermissionMenuOpen((open) => !open); setAddMenuOpen(false); setSkillSubmenuOpen(false) }}><ShieldCheck size={14} /><span>{permissionLabel(selectedPermissionMode)}</span><ChevronRight size={12} /></button>
                      {permissionMenuOpen && <div className="capability-popover permission-popover" role="menu" aria-label="权限模式">
                        <p>权限</p>
                        {permissionOptions.map((option) => <button key={option.value} type="button" role="menuitemradio" aria-checked={selectedPermissionMode === option.value} className={selectedPermissionMode === option.value ? 'selected' : ''} onClick={() => selectPermissionMode(option.value)}><span>{option.label}</span>{selectedPermissionMode === option.value && <Check size={14} />}</button>)}
                      </div>}
                    </div>
                  </div>
                  <div className="composer-actions">
                    {draftActive ? <ContextUsageRing context={emptyDraftContext} /> : context.error ? <button type="button" className="context-state context-error" aria-label="上下文占用读取失败，点击重试" title={context.error} onClick={() => void context.reload()}><AlertCircle size={15} /></button> : context.loading || !context.data ? <span className="context-state" role="status" aria-label="正在读取上下文占用"><LoaderCircle className="spin" size={15} /></span> : <ContextUsageRing context={context.data} />}
                    <div className="session-settings-picker" ref={settingsMenuRef} title={settingsLocked ? '当前运行结束或审批完成后才能切换模型和思考强度' : undefined}>
                      <button
                        ref={settingsTriggerRef}
                        type="button"
                        className="session-settings-trigger"
                        aria-label={`当前模型 ${modelButtonLabel}，思考强度 ${thinkingLevelLabels[effectiveThinking]}。点击更改`}
                        aria-haspopup="menu"
                        aria-expanded={settingsMenuOpen}
                        disabled={settingsSaving || settingsLocked || sending}
                        onClick={() => { setSettingsMenuOpen((open) => !open); setSettingsSubmenu(null) }}
                      >
                        {settingsSaving && <LoaderCircle className="spin settings-spinner" size={13} />}
                        <span>{modelButtonLabel}</span><strong>{thinkingLevelLabels[effectiveThinking]}</strong><ChevronRight size={12} aria-hidden="true" />
                      </button>
                      {settingsMenuOpen && <div className="session-settings-popover" role="menu" aria-label="模型和思考设置">
                        <button type="button" className={settingsSubmenu === 'model' ? 'active' : ''} role="menuitem" aria-haspopup="menu" aria-expanded={settingsSubmenu === 'model'} onMouseEnter={() => openSettingsSubmenu('model')} onKeyDown={(event) => { if (event.key === 'ArrowRight') { event.preventDefault(); openSettingsSubmenu('model', true) } }} onClick={() => openSettingsSubmenu('model', true)}>
                          <span>模型</span><small>{modelButtonLabel}</small><ChevronRight size={13} aria-hidden="true" />
                        </button>
                        {settingsSubmenu === 'model' && <div className="session-settings-submenu" ref={modelSubmenuRef} role="menu" aria-label="选择模型">
                          <p>模型</p>
                          <button type="button" role="menuitemradio" aria-checked={selectedModelValue === ''} className={selectedModelValue === '' ? 'selected' : ''} onClick={() => selectModel('')}>
                            <span><strong>自动</strong><small>{automaticModel || '默认模型'}{automaticSessionConnection?.name ? ` · ${automaticSessionConnection.name}` : ''}</small></span>{selectedModelValue === '' && <Check size={14} aria-hidden="true" />}
                          </button>
                          {modelOptions.map((option) => <button key={option.value} type="button" role="menuitemradio" aria-checked={selectedModelValue === option.value} className={selectedModelValue === option.value ? 'selected' : ''} onClick={() => selectModel(option.value)}>
                            <span><strong>{option.label}</strong><small>{option.connection}</small></span>{selectedModelValue === option.value && <Check size={14} aria-hidden="true" />}
                          </button>)}
                        </div>}
                        <button type="button" className={settingsSubmenu === 'thinking' ? 'active' : ''} role="menuitem" aria-haspopup="menu" aria-expanded={settingsSubmenu === 'thinking'} onMouseEnter={() => openSettingsSubmenu('thinking')} onKeyDown={(event) => { if (event.key === 'ArrowRight') { event.preventDefault(); openSettingsSubmenu('thinking', true) } }} onClick={() => openSettingsSubmenu('thinking', true)}>
                          <span>推理强度</span><small>{thinkingLevelLabels[effectiveThinking]}</small><ChevronRight size={13} aria-hidden="true" />
                        </button>
                        {settingsSubmenu === 'thinking' && <div className="session-settings-submenu" ref={thinkingSubmenuRef} role="menu" aria-label="选择推理强度">
                          <p>推理强度</p>
                          {thinkingOptions.map((option) => <button key={option.value} type="button" role="menuitemradio" aria-checked={selectedThinkingValue === option.value} className={selectedThinkingValue === option.value ? 'selected' : ''} onClick={() => selectThinking(option.value)}>
                            <span><strong>{option.label}</strong>{option.hint && <small>{option.hint}</small>}</span>{selectedThinkingValue === option.value && <Check size={14} aria-hidden="true" />}
                          </button>)}
                        </div>}
                      </div>}
                    </div>
                    <button className="send-button" aria-label="发送" disabled={sending || settingsSaving || capabilitySaving || settingsLocked || !composer.trim()}>{sending ? <LoaderCircle className="spin" /> : <Send />}</button>
                  </div>
                </div>
              </form>
            </> : <EmptyState icon={MessageSquare} title="开始新对话" description="创建一个临时草稿；首次发送后才会保存为任务或项目对话。" action={<button className="button button-primary" onClick={beginDraft}>新建对话</button>} />}
          </section>
          {childPanelOpen && <ChildAgentPanel
            tasks={childTasks.data}
            loading={childTasks.loading}
            error={childTasks.error}
            selectedTask={activeChildTask}
            run={childTaskRun}
            events={childTaskEvents.data}
            eventsLoading={childTaskEvents.loading}
            eventsError={childTaskEvents.error}
            onClose={() => setChildPanelOpen(false)}
            onSelect={setSelectedChildTaskId}
            onRetry={() => { void childTasks.reload(); void childTaskEvents.reload() }}
          />}
      </div>
    </div>
  )
}

function childTaskStatusLabel(status?: string) {
  return statusText[(status || '').toLowerCase()] ?? status ?? '处理中'
}

function childTaskOutput(task?: DelegatedTask) {
  const output = task?.result?.output
  if (typeof output === 'string' && output.trim()) return output
  const error = task?.result?.error
  if (typeof error === 'string' && error.trim()) return error
  return ''
}

function ChildAgentPanel({
  tasks,
  loading,
  error,
  selectedTask,
  run,
  events,
  eventsLoading,
  eventsError,
  onClose,
  onSelect,
  onRetry,
}: {
  tasks: DelegatedTask[]
  loading: boolean
  error: string
  selectedTask?: DelegatedTask
  run?: Run
  events: RunEvent[]
  eventsLoading: boolean
  eventsError: string
  onClose: () => void
  onSelect: (taskId: string) => void
  onRetry: () => void
}) {
  const output = childTaskOutput(selectedTask)
  const toolEvents = events.filter((event) => ['tool_started', 'tool_finished', 'tool_result'].includes(event.type || event.event_type || ''))
  return <aside className="child-agent-panel" aria-label="子 Agent 工作详情">
    <header className="child-panel-header"><div><span className="eyebrow">协作执行</span><strong>子 Agent</strong></div><button type="button" className="icon-button" onClick={onClose} aria-label="收起子 Agent 侧栏"><X size={16} /></button></header>
    {error ? <ErrorState message={error} onRetry={onRetry} /> : loading && !tasks.length ? <LoadingState label="正在读取子 Agent…" /> : !tasks.length ? <EmptyState icon={Users} title="子 Agent 正在启动" description="任务创建后会显示在这里。" /> : <>
      <div className="child-task-list" role="list" aria-label="本次调用的子 Agent">
        {tasks.map((task) => {
          const selected = task.id === selectedTask?.id
          const agent = task.result?.agent
          const agentName = agent && typeof agent === 'object' && typeof (agent as Record<string, unknown>).name === 'string'
            ? String((agent as Record<string, unknown>).name)
            : task.child_agent_name || '子 Agent'
          return <button key={task.id} type="button" className={selected ? 'selected' : ''} onClick={() => onSelect(task.id)}>
            <span className="child-task-avatar"><Bot size={14} /></span><span><strong>{agentName}</strong><small>{task.title}</small></span><StatusBadge status={task.status} />
          </button>
        })}
      </div>
      {selectedTask && <section className="child-task-detail">
        <header><div><strong>{selectedTask.title}</strong><small>{childTaskStatusLabel(selectedTask.status)}</small></div><StatusBadge status={selectedTask.status} /></header>
        <dl className="child-task-stats"><div><dt>步骤</dt><dd>{numberFromRecord(selectedTask.result, 'steps') ?? run?.step_count ?? run?.current_step ?? 0}</dd></div><div><dt>工具</dt><dd>{numberFromRecord(selectedTask.result, 'tool_calls') ?? run?.tool_calls ?? 0}</dd></div></dl>
        {selectedTask.description && <section className="child-detail-block"><strong>任务</strong><p>{selectedTask.description}</p></section>}
        {output && <section className="child-detail-block"><strong>{selectedTask.status === 'completed' ? '结果' : '状态说明'}</strong><pre>{output}</pre></section>}
        <section className="child-detail-block child-events"><strong>工作过程</strong>{eventsError ? <p className="inline-error">{eventsError}</p> : eventsLoading ? <p>正在读取运行事件…</p> : toolEvents.length ? <ol>{toolEvents.map((event, index) => <li key={event.id || index}><span>{event.type || event.event_type}</span><small>{formatDate(event.created_at)}</small></li>)}</ol> : <p>暂未记录工具调用。</p>}</section>
      </section>}
    </>}
  </aside>
}

function MessageBubble({ message }: { message: Message }) {
  const isTool = message.role === 'tool' || !!message.tool_name
  const isDelegatedChild = message.metadata?.delegated_child === true
  const childAgentName = typeof message.metadata?.child_agent_name === 'string'
    ? message.metadata.child_agent_name
    : '子 Agent'
  const speaker = message.role === 'user'
    ? '你'
    : isTool
      ? message.tool_name || '工具结果'
      : isDelegatedChild
        ? `${childAgentName}（子 Agent）`
        : 'PGAgent'
  return (
    <article className={`message ${message.role} ${isTool ? 'tool-message' : ''}`}>
      <div className="message-avatar">{message.role === 'user' ? '你' : isTool ? <SquareTerminal size={16} /> : <Sparkles size={16} />}</div>
      <div className="message-body"><div className="message-meta"><strong>{speaker}</strong><time>{formatDate(message.created_at)}</time></div><div className="message-content">{message.content}</div>{message.status && <StatusBadge status={message.status} />}</div>
    </article>
  )
}

function CompletedThoughtTimeline({ runId, timeline }: { runId: string; timeline: ThoughtTimelineState }) {
  const [expanded, setExpanded] = useState(false)
  const duration = formatThoughtDuration(timeline.elapsedMs)
  const hasDetails = timeline.tools.length > 0

  return <article className={`completed-thought ${hasDetails && expanded ? 'expanded' : ''} ${hasDetails ? '' : 'no-details'}`}>
    {hasDetails ? <button
      type="button"
      className="completed-thought-toggle"
      aria-expanded={expanded}
      aria-controls={`thought-details-${runId}`}
      onClick={() => setExpanded((value) => !value)}
    >
      <span className="completed-thought-duration">已处理 {duration}</span>
      <ChevronRight className="completed-thought-chevron" size={13} aria-hidden="true" />
    </button> : <span className="completed-thought-duration completed-thought-static">已处理 {duration}</span>}
    {hasDetails && expanded && <div id={`thought-details-${runId}`} className="thought-tool-list" aria-label="工具调用">{timeline.tools.map((tool) => <p key={tool.id} className={`thought-tool ${tool.status}`}><span>{tool.name.startsWith('Web') ? '⌁' : '→'}</span><strong>{tool.name}</strong>{tool.target && <code title={tool.target}>{tool.target}</code>}{tool.status === 'running' && <i aria-label="运行中" />}</p>)}</div>}
  </article>
}

function LiveAssistantMessage({ liveRun }: { liveRun: LiveRunState }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (liveRun.thought.startedAt === null || liveRun.thought.finished) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 100)
    return () => window.clearInterval(timer)
  }, [liveRun.thought.finished, liveRun.thought.startedAt])
  const liveThoughtMs = liveRun.thought.startedAt === null ? 0 : Math.max(0, now - liveRun.thought.startedAt)
  const phase = liveRun.phase.includes('子 Agent')
    ? liveRun.phase
    : !liveRun.thought.finished && liveRun.status !== 'awaiting_approval' && liveRun.thinkingStatus
    ? `${liveRun.thinkingStatus} ${formatLiveThinkingDuration(liveThoughtMs)}`
    : liveRun.phase || '已完成'
  return (
    <article className={`message assistant live-message ${liveRun.status === 'terminal' ? 'live-message-terminal' : ''}`}>
      <div className="message-avatar"><Sparkles size={16} /></div>
      <div className="message-body">
        <div className="message-meta"><strong>PGAgent</strong><span className="live-phase"><i aria-hidden="true" />{phase}</span></div>
        {liveRun.draft && <div className="message-content">{liveRun.draft}</div>}
        {liveRun.error && <p className="live-error">{liveRun.error}</p>}
      </div>
    </article>
  )
}

function ApprovalCard({ approval, deciding, onDecision }: { approval: Approval; deciding: boolean; onDecision: (id: string, decision: 'approve' | 'reject', runId: string) => void }) {
  const runId = stringId(approval.run_id)
  return (
    <article className="approval-card">
      <header><span><ShieldCheck size={17} /></span><div><strong>需要你的批准</strong><p>Agent 请求执行有副作用的工具</p></div><StatusBadge status={approval.status || 'pending'} /></header>
      <div className="approval-command"><span>{approval.tool_name || 'unknown_tool'}</span><pre>{JSON.stringify(approval.arguments ?? {}, null, 2)}</pre></div>
      {approval.reason && <p className="approval-reason">理由：{approval.reason}</p>}
      <footer><button className="button button-danger" disabled={deciding || !runId} onClick={() => onDecision(approval.id, 'reject', runId)}><XCircle size={15} />拒绝</button><button className="button button-primary" disabled={deciding || !runId} onClick={() => onDecision(approval.id, 'approve', runId)}>{deciding ? <LoaderCircle className="spin" size={15} /> : <CheckCircle2 size={15} />}允许本次</button></footer>
    </article>
  )
}

function RunsPage() {
  const runs = useApiData<Run[]>([], () => api.list<Run>('/api/runs', ['runs']), [])
  const [selected, setSelected] = useState<Run | null>(null)
  const [filter, setFilter] = useState('all')
  const filtered = filter === 'all' ? runs.data : runs.data.filter((run) => run.status === filter)
  return (
    <div className="page">
      <PageHeader eyebrow="OBSERVABILITY" title="运行记录" description="每一次计划、工具调用、重试和停止原因都保留可追溯记录。" action={<button className="button button-secondary" onClick={runs.reload}><RefreshCw size={15} />刷新</button>} />
      <div className="filter-bar" role="group" aria-label="运行状态筛选">{[['all', '全部'], ['running', '运行中'], ['completed', '已完成'], ['failed', '失败'], ['stopped', '已停止']].map(([value, label]) => <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>{label}</button>)}</div>
      {runs.error ? <ErrorState message={runs.error} onRetry={runs.reload} /> : runs.loading ? <LoadingState /> : filtered.length ? (
        <section className="card run-table-card"><div className="table-head"><span>任务</span><span>状态</span><span>步数 / 工具</span><span>更新时间</span></div>{filtered.map((run) => <RunRow key={run.id} run={run} onClick={() => setSelected(run)} />)}</section>
      ) : <EmptyState icon={History} title="没有符合条件的运行" description="运行 Agent 后，这里会显示状态和完整停止原因。" />}
      {selected && <SlidePanel title={selected.title || `运行 ${selected.id.slice(0, 8)}`} description="运行状态、资源使用和事件时间线" onClose={() => setSelected(null)}><RunDetails run={selected} /></SlidePanel>}
    </div>
  )
}

function RunDetails({ run }: { run: Run }) {
  const events = useApiData<Run['events']>([], () => api.list<NonNullable<Run['events']>[number]>(`/api/runs/${run.id}/events`, ['events']), [run.id])
  const timeline = run.events?.length ? run.events : events.data
  return (
    <div className="run-detail">
      <div className="detail-hero"><StatusBadge status={run.status} /><strong>{statusText[run.phase || run.status || ''] ?? run.phase ?? run.status ?? '暂无阶段信息'}</strong></div>
      <dl className="detail-grid"><div><dt>执行步数</dt><dd>{run.step_count ?? run.current_step ?? 0}</dd></div><div><dt>工具调用</dt><dd>{run.tool_call_count ?? run.tool_calls ?? 0}</dd></div></dl>
      <p className="run-usage-note"><Database size={14} />当前后端暂未提供单次运行的 Token 明细，可前往“用量统计”查看全局与按模型汇总。</p>
      {run.stop_reason && <div className="stop-reason"><AlertCircle size={17} /><div><strong>停止原因</strong><p>{run.stop_reason}</p></div></div>}
      <h3>事件时间线</h3>
      {events.error ? <ErrorState message={events.error} onRetry={events.reload} /> : events.loading ? <LoadingState /> : timeline?.length ? <div className="timeline">{timeline.map((event, index) => <div key={event.id || index}><span /><div><strong>{event.message || event.type || event.event_type || event.phase || '运行事件'}</strong><small>{formatDate(event.created_at)}</small></div></div>)}</div> : <EmptyState icon={Activity} title="暂无事件详情" description="后端记录运行事件后会在这里展示。" />}
    </div>
  )
}

function formatTokens(value = 0) {
  return new Intl.NumberFormat('zh-CN', { notation: value >= 10000 ? 'compact' : 'standard', maximumFractionDigits: 2 }).format(value)
}

function formatCost(value = 0) {
  return `$${value.toFixed(value >= 1 ? 4 : 6)}`
}

function usageRate(value = 0) {
  const percent = value <= 1 ? value * 100 : value
  return Math.min(100, Math.max(0, percent))
}

function UsagePage() {
  const emptySummary: UsageSummary = { total_requests: 0, input_tokens: 0, output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0, total_tokens: 0, total_cost_usd: 0, cache_hit_rate: 0 }
  const today = usageDateKey(new Date())
  const [startDate, setStartDate] = useState(today)
  const [endDate, setEndDate] = useState(today)
  const [datePreset, setDatePreset] = useState<UsageDatePreset>('today')
  const [rangeAnchor, setRangeAnchor] = useState(() => new Date())
  const range = usageDateRange(startDate, endDate, datePreset, rangeAnchor)
  const summary = useApiData<UsageSummary>(emptySummary, () => api.get<UsageSummary>(`/api/usage/summary${range}`), [range])
  const models = useApiData<ModelUsage[]>([], () => api.list<ModelUsage>(`/api/usage/models${range}`), [range])
  const sessions = useApiData<UsageSession[]>([], () => api.list<UsageSession>(`/api/usage/sessions${range}`), [range])
  const projects = useApiData<UsageWorkspace[]>([], () => api.list<UsageWorkspace>(`/api/usage/workspaces${range}`), [range])
  const sessionUsage = sessions.data.map((item): UsageBreakdownItem => ({
    id: item.session_id ?? 'unassigned-session',
    title: item.title || '未关联对话',
    requests: item.requests,
    tokens: item.tokens,
    total_cost_usd: item.total_cost_usd,
    avg_cost_usd: item.avg_cost_usd,
    cache_hit_rate: item.cache_hit_rate,
  }))
  const workspaceUsage = projects.data.map((item): UsageBreakdownItem => ({
    id: item.workspace_id ?? 'unassigned-workspace',
    title: item.name || '未关联项目',
    path: item.path || undefined,
    requests: item.requests,
    tokens: item.tokens,
    total_cost_usd: item.total_cost_usd,
    avg_cost_usd: item.avg_cost_usd,
    cache_hit_rate: item.cache_hit_rate,
  }))
  const cacheRate = usageRate(summary.data.cache_hit_rate)
  function setPreset(preset: QuickUsageDatePreset) {
    const anchor = new Date()
    const { start, end } = usageDatePresetBounds(preset, anchor)
    setDatePreset(preset)
    setRangeAnchor(anchor)
    setStartDate(usageDateKey(start))
    setEndDate(usageDateKey(end))
  }
  function reloadUsage() {
    if (datePreset !== 'custom') {
      setRangeAnchor(new Date())
      return
    }
    void Promise.all([summary.reload(), models.reload(), sessions.reload(), projects.reload()])
  }
  const usageCards = [
    { label: '输入 Token', value: formatTokens(summary.data.input_tokens), icon: ArrowRight },
    { label: '输出 Token', value: formatTokens(summary.data.output_tokens), icon: Sparkles },
    { label: '缓存创建', value: formatTokens(summary.data.cache_creation_tokens), icon: Database },
    { label: '缓存命中', value: formatTokens(summary.data.cache_read_tokens), icon: CheckCircle2 },
  ]
  return (
    <div className="page usage-page">
      <PageHeader eyebrow="TOKEN & COST" title="用量统计" description="按时间范围查看模型、对话与项目的 Token、缓存和成本。" action={<button className="button button-secondary" onClick={reloadUsage}><RefreshCw size={15} />刷新</button>} />
      <section className="usage-date-filter card" aria-label="用量日期范围">
        <div className="usage-date-filter-head"><div><CalendarDays size={16} /><span>统计日期</span></div><small>{usageRangeLabel(startDate, endDate, datePreset)}</small></div>
        <div className="usage-date-presets" role="group" aria-label="快捷日期范围">{usageDateOptions.map(({ id, label }) => <button key={id} type="button" className={datePreset === id ? 'active' : ''} onClick={() => setPreset(id)}>{label}</button>)}</div>
        <div className="usage-date-inputs"><label><span>开始日期</span><input type="date" value={startDate} max={endDate || undefined} onChange={(event) => { setDatePreset('custom'); setStartDate(event.target.value) }} /></label><span className="usage-date-separator">至</span><label><span>结束日期</span><input type="date" value={endDate} min={startDate || undefined} max={today} onChange={(event) => { setDatePreset('custom'); setEndDate(event.target.value) }} /></label></div>
      </section>
      {summary.error && !summary.data.total_requests ? <ErrorState message={summary.error} onRetry={summary.reload} /> : summary.initialLoading ? <LoadingState /> : <>
        <section className="usage-hero card">
          {summary.refreshing && <UsageRefreshBadge />}
          <div className="usage-total"><span className="usage-bolt"><ChartNoAxesCombined size={22} /></span><div><p>实际消耗 Token</p><strong>{summary.data.total_tokens.toLocaleString()}</strong><small>≈ {formatTokens(summary.data.total_tokens)}</small></div></div>
          <dl className="usage-mini-totals"><div><dt>成功模型响应数</dt><dd>{summary.data.total_requests.toLocaleString()}</dd></div><div><dt>总成本</dt><dd>{formatCost(summary.data.total_cost_usd)}</dd></div></dl>
          <div className="usage-card-grid">{usageCards.map(({ label, value, icon: Icon }) => <article key={label}><span><Icon size={15} />{label}</span><strong>{value}</strong></article>)}</div>
          <div className="cache-meter"><div><span>缓存命中率</span><strong>{cacheRate.toFixed(1)}%</strong></div><i><b style={{ width: `${cacheRate}%` }} /></i></div>
        </section>
      </>}
      <section className="usage-models card">
        <div className="card-heading"><div><p className="eyebrow">BY MODEL</p><h2>模型明细</h2></div>{models.refreshing && <UsageRefreshBadge />}</div>
        {models.error && !models.data.length ? <ErrorState message={models.error} onRetry={models.reload} /> : models.initialLoading ? <LoadingState /> : models.data.length ? <div className="usage-table-wrap"><table><thead><tr><th>模型 / 提供商</th><th>响应数</th><th>Tokens</th><th>命中率</th><th>总成本</th><th>平均成本</th></tr></thead><tbody>{models.data.map((model) => <tr key={`${model.provider || 'unknown'}:${model.model_id}`}><th><span>{model.model_id || 'unknown'}</span><small>{model.provider || 'unknown provider'}</small></th><td>{model.requests.toLocaleString()}</td><td>{model.tokens.toLocaleString()}</td><td>{usageRate(model.cache_hit_rate).toFixed(1)}%</td><td>{formatCost(model.total_cost_usd)}</td><td>{formatCost(model.avg_cost_usd)}</td></tr>)}</tbody></table></div> : <EmptyState icon={ChartNoAxesCombined} title="该日期范围暂无用量" description="调整日期范围，或在该范围内完成一次模型调用后再查看。" />}
      </section>
      <UsageBreakdown title="对话用量" eyebrow="BY CONVERSATION" emptyDescription="该日期范围内暂无产生模型调用的对话。" data={sessionUsage} initialLoading={sessions.initialLoading} refreshing={sessions.refreshing} error={sessions.error} onRetry={sessions.reload} />
      <UsageBreakdown title="项目用量" eyebrow="BY PROJECT" emptyDescription="该日期范围内暂无项目关联的模型调用。" data={workspaceUsage} initialLoading={projects.initialLoading} refreshing={projects.refreshing} error={projects.error} onRetry={projects.reload} />
    </div>
  )
}

function UsageRefreshBadge() {
  return <span className="usage-refresh-badge" role="status"><LoaderCircle className="spin" size={12} />更新中</span>
}

function UsageBreakdown({ title, eyebrow, emptyDescription, data, initialLoading, refreshing, error, onRetry }: { title: string; eyebrow: string; emptyDescription: string; data: UsageBreakdownItem[]; initialLoading: boolean; refreshing: boolean; error: string; onRetry: () => void }) {
  return <section className="usage-models card">
    <div className="card-heading"><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2></div>{refreshing && <UsageRefreshBadge />}</div>
    {error && !data.length ? <ErrorState message={error} onRetry={onRetry} /> : initialLoading ? <LoadingState /> : data.length ? <div className="usage-table-wrap"><table><thead><tr><th>名称</th><th>响应数</th><th>Tokens</th><th>命中率</th><th>总成本</th><th>平均成本</th></tr></thead><tbody>{data.map((item) => <tr key={item.id}><th><span title={item.path || item.title}>{item.title}</span>{item.path && <small title={item.path}>{item.path}</small>}</th><td>{item.requests.toLocaleString()}</td><td>{item.tokens.toLocaleString()}</td><td>{usageRate(item.cache_hit_rate).toFixed(1)}%</td><td>{formatCost(item.total_cost_usd)}</td><td>{formatCost(item.avg_cost_usd)}</td></tr>)}</tbody></table></div> : <EmptyState icon={ChartNoAxesCombined} title="暂无用量记录" description={emptyDescription} />}
  </section>
}

// Reserved for the future multi-user collaboration feature. It is intentionally
// not routed or shown in navigation during the current release.
export function TeamsPage() {
  const tasks = useApiData<TeamTask[]>([], () => api.list<TeamTask>('/api/teams/tasks', ['tasks']), [])
  const agents = useApiData<AgentProfile[]>([], () => api.list<AgentProfile>('/api/agents', ['agents']), [])
  const [panelOpen, setPanelOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  const assignableAgents = agents.data.filter((agent) => !agent.is_default)

  async function createTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = new FormData(event.currentTarget); setSaving(true); setFormError('')
    try { await api.post('/api/teams/tasks', { title: form.get('title'), description: form.get('description'), priority: form.get('priority'), assignee_agent_id: form.get('assignee_agent_id') || undefined }); setPanelOpen(false); await tasks.reload() }
    catch (error) { setFormError(describeError(error)) } finally { setSaving(false) }
  }
  const columns = [{ key: 'todo', label: '待认领' }, { key: 'in_progress', label: '进行中' }, { key: 'review', label: '待验收' }, { key: 'completed', label: '已完成' }]
  return (
    <div className="page">
      <PageHeader eyebrow="MULTI-AGENT COORDINATION" title="团队任务" description="SQLite 任务板是唯一事实源；Agent 通过结构化信封和租约安全协作。" action={<button className="button button-primary" onClick={() => setPanelOpen(true)}><Plus size={16} />创建任务</button>} />
      <div className="team-principles"><div><Network size={18} /><span><strong>结构化通信</strong><small>任务、进度、产物与验收消息持久化</small></span></div><div><KeyRound size={18} /><span><strong>租约 + CAS</strong><small>避免两个 Worker 同时认领同一任务</small></span></div><div><ShieldCheck size={18} /><span><strong>独立上下文</strong><small>Lead 只接收结论与产物引用</small></span></div></div>
      {tasks.error ? <ErrorState message={tasks.error} onRetry={tasks.reload} /> : tasks.loading ? <LoadingState /> : (
        <section className="kanban">
          {columns.map((column) => { const columnTasks = tasks.data.filter((task) => (task.status || 'todo') === column.key); return <div className="kanban-column" key={column.key}><header><span>{column.label}</span><b>{columnTasks.length}</b></header><div className="kanban-list">{columnTasks.map((task) => <article className="task-card" key={task.id}><div className="task-priority">{task.priority || 'normal'}</div><h3>{task.title}</h3><p>{task.description || '暂无任务说明'}</p><footer><span><Bot size={14} />{task.assignee_name || '尚未指派'}</span><small>v{task.version ?? 1}</small></footer></article>)}{!columnTasks.length && <div className="column-empty">暂无任务</div>}</div></div> })}
        </section>
      )}
      {panelOpen && <SlidePanel title="创建团队任务" description="任务将写入持久化任务板，供 Lead 或 Worker 认领。" onClose={() => setPanelOpen(false)}><form className="panel-form" onSubmit={createTask}><Field label="任务标题"><input name="title" required placeholder="清晰描述可独立交付的子任务" autoFocus /></Field><Field label="任务说明"><textarea name="description" rows={5} placeholder="包含输入、约束和验收标准" /></Field><div className="form-row"><Field label="优先级"><select name="priority" defaultValue="normal"><option value="low">低</option><option value="normal">普通</option><option value="high">高</option></select></Field><Field label="指派子 Agent"><select name="assignee_agent_id"><option value="">暂不指派</option>{assignableAgents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></Field></div>{formError && <p className="form-error">{formError}</p>}<div className="form-actions"><button type="button" className="button button-secondary" onClick={() => setPanelOpen(false)}>取消</button><button className="button button-primary" disabled={saving}>{saving && <LoaderCircle className="spin" size={15} />}创建</button></div></form></SlidePanel>}
    </div>
  )
}

function ModelsPage() {
  const connections = useApiData<Connection[]>([], () => api.list<Connection>('/api/connections', ['connections']), [])
  const [panelOpen, setPanelOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [discovering, setDiscovering] = useState('')
  const [formError, setFormError] = useState('')
  const [discoveryError, setDiscoveryError] = useState<Record<string, string>>({})

  async function createConnection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = new FormData(event.currentTarget); setSaving(true); setFormError('')
    try {
      const manualModel = String(form.get('default_model') || '').trim()
      await api.post('/api/connections', { name: form.get('name'), base_url: form.get('base_url'), api_key: form.get('api_key'), provider: form.get('provider'), default_model: manualModel || undefined, manual_models: manualModel ? [manualModel] : [], thinking_level: form.get('thinking_level') })
      setPanelOpen(false); await connections.reload()
    } catch (error) { setFormError(describeError(error)) } finally { setSaving(false) }
  }

  async function discover(connection: Connection) {
    setDiscovering(connection.id); setDiscoveryError((value) => ({ ...value, [connection.id]: '' }))
    try { await api.post(`/api/connections/${connection.id}/test`); await connections.reload() }
    catch (error) { setDiscoveryError((value) => ({ ...value, [connection.id]: describeError(error) })) } finally { setDiscovering('') }
  }

  async function updateThinking(connection: Connection, level: string) {
    setDiscoveryError((value) => ({ ...value, [connection.id]: '' }))
    try { await api.patch(`/api/connections/${connection.id}`, { thinking_level: level }); await connections.reload() }
    catch (error) { setDiscoveryError((value) => ({ ...value, [connection.id]: describeError(error) })) }
  }

  return (
    <div className="page">
      <PageHeader eyebrow="MODEL GATEWAY" title="模型设置" description="只需 Base URL 与 API Key；PGAgent 会发现模型并统一思考强度。" action={<button className="button button-primary" onClick={() => setPanelOpen(true)}><Plus size={16} />添加连接</button>} />
      <div className="security-note"><KeyRound size={19} /><div><strong>密钥不会明文保存到 SQLite</strong><p>API Key 进入 Windows 凭据存储，数据库仅保留引用和末尾提示。</p></div></div>
      {connections.error ? <ErrorState message={connections.error} onRetry={connections.reload} /> : connections.loading ? <LoadingState /> : connections.data.length ? (
        <section className="connection-list">
          {connections.data.map((connection) => (
            <article className="connection-card" key={connection.id}>
              <header><div className="provider-icon"><Sparkles size={20} /></div><div><h2>{connection.name}</h2><p>{connection.provider || 'OpenAI Compatible'}</p></div><StatusBadge status={connection.status || 'unknown'} /></header>
              <dl><div><dt>Base URL</dt><dd>{connection.base_url || '—'}</dd></div><div><dt>API Key</dt><dd>{connection.api_key_hint || '已安全保存'}</dd></div><div><dt>默认模型</dt><dd>{connection.default_model || '尚未选择'}</dd></div></dl>
              <div className="model-block"><div className="model-block-head"><strong>可用模型</strong><button className="button button-quiet" onClick={() => discover(connection)} disabled={discovering === connection.id}>{discovering === connection.id ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}发现模型</button></div>{(connection.models || connection.discovered_models || connection.manual_models)?.length ? <div className="model-tags">{(connection.models || [...(connection.discovered_models || []), ...(connection.manual_models || [])]).map((model) => <span className={model === connection.default_model ? 'selected' : ''} key={model}>{model}</span>)}</div> : <p className="muted">尚未获取模型列表，可点击发现或使用手动模型 ID。</p>}</div>
              <div className="thinking-row"><div><strong>思考强度</strong><small>不同提供商会映射到各自支持的参数</small></div><select aria-label={`${connection.name} 思考强度`} value={connection.thinking_level || 'auto'} onChange={(event) => void updateThinking(connection, event.target.value)}><option value="off">关闭</option><option value="auto">自动</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高</option></select></div>
              {(discoveryError[connection.id] || connection.last_error) && <p className="inline-error"><AlertCircle size={14} />{discoveryError[connection.id] || connection.last_error}</p>}
            </article>
          ))}
        </section>
      ) : <EmptyState icon={Sparkles} title="添加模型连接" description="支持 OpenRouter、DeepSeek 和大多数 OpenAI 兼容中转服务。" action={<button className="button button-primary" onClick={() => setPanelOpen(true)}><Plus size={16} />添加连接</button>} />}
      {panelOpen && <SlidePanel title="添加模型连接" description="保存后会尝试请求 /models；失败时仍可使用手动模型 ID。" onClose={() => setPanelOpen(false)}><form className="panel-form" onSubmit={createConnection} autoComplete="off"><div className="form-row"><Field label="连接名称"><input name="name" required placeholder="例如：DeepSeek" autoFocus /></Field><Field label="提供商"><select name="provider" defaultValue="openai_compatible"><option value="openai_compatible">OpenAI 兼容</option><option value="openrouter">OpenRouter</option><option value="deepseek">DeepSeek</option></select></Field></div><Field label="Base URL"><input name="base_url" type="url" required placeholder="https://api.example.com/v1" /></Field><Field label="API Key" hint="只发送给你填写的 Base URL，并写入系统凭据存储"><input name="api_key" type="password" required placeholder="sk-…" autoComplete="new-password" /></Field><div className="form-row"><Field label="手动模型 ID" hint="当 /models 不可用时使用"><input name="default_model" placeholder="例如：deepseek-chat" /></Field><Field label="思考强度"><select name="thinking_level" defaultValue="auto"><option value="off">关闭</option><option value="auto">自动</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高</option></select></Field></div>{formError && <p className="form-error" role="alert">{formError}</p>}<div className="form-actions"><button type="button" className="button button-secondary" onClick={() => setPanelOpen(false)}>取消</button><button className="button button-primary" disabled={saving}>{saving && <LoaderCircle className="spin" size={15} />}保存并检测</button></div></form></SlidePanel>}
    </div>
  )
}

export default function App() {
  return <AppShell />
}
