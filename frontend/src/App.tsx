import {
  Activity,
  AlertCircle,
  ArrowRight,
  Bot,
  Box,
  Check,
  CheckCircle2,
  ChevronRight,
  Clock3,
  ChartNoAxesCombined,
  Database,
  FolderOpen,
  Folder,
  History,
  KeyRound,
  LayoutDashboard,
  LoaderCircle,
  Menu,
  MessageSquare,
  Network,
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
  Users,
  Workflow,
  X,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import { type FormEvent, type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api, apiUrl, describeError } from './api'
import { modelSelectionPayload, resolveEffectiveThinking, shortModelLabel, thinkingLevelLabels } from './composerSettings'
import { buildContextUsageView } from './contextUsage'
import { availableConnectionModels, resolveEffectiveModelSettings } from './modelSettings'
import { appendAssistantDelta, isCurrentSessionRun, isTerminalRunStatus, isTerminalRunStreamEvent, parseRunStreamEvent, rememberRunStreamEvent, runStatusPhase, runStreamPhase, shouldMarkApprovalResuming, visibleSessionItems, type RunStreamEvent } from './sessionStream'
import type {
  AgentProfile,
  Approval,
  Connection,
  DashboardData,
  Health,
  Message,
  Run,
  Session,
  SessionContext,
  TeamTask,
  ThinkingLevel,
  UsageSummary,
  ModelUsage,
  FolderSelection,
  Workspace,
} from './types'
import './App.css'

type LoadState<T> = { data: T; loading: boolean; error: string }
type LiveRunState = { runId: string; phase: string; draft: string; status: 'idle' | 'connecting' | 'live' | 'fallback' | 'awaiting_approval' | 'terminal'; error: string }
type OwnedSessionMessages = { ownerSessionId: string; items: Message[] }

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
  'tool_finished',
  'approval_requested',
  'approval_granted',
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

  return { ...state, reload, refresh, setState }
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
  { path: '/workspaces', label: '工作区', icon: Folder },
  { path: '/agents', label: 'Agent', icon: Bot },
  { path: '/sessions', label: '会话', icon: MessageSquare },
  { path: '/runs', label: '运行记录', icon: History },
  { path: '/usage', label: '用量统计', icon: ChartNoAxesCombined },
  { path: '/teams', label: '团队任务', icon: Users },
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
          <Route path="/workspaces" element={<WorkspacesPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/usage" element={<UsagePage />} />
          <Route path="/teams" element={<TeamsPage />} />
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

function WorkspacesPage() {
  const workspaces = useApiData<Workspace[]>([], () => api.list<Workspace>('/api/workspaces', ['workspaces']), [])
  const [panelOpen, setPanelOpen] = useState(false)
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const [selectedPath, setSelectedPath] = useState('')
  const [pickingFolder, setPickingFolder] = useState(false)

  function openPanel() {
    setSelectedPath('')
    setFormError('')
    setPanelOpen(true)
  }

  async function selectFolder() {
    setPickingFolder(true); setFormError('')
    try {
      const result = await api.post<FolderSelection>('/api/system/select-folder')
      if (result.path) setSelectedPath(result.path)
    } catch (error) { setFormError(describeError(error)) } finally { setPickingFolder(false) }
  }

  async function createWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setSaving(true); setFormError('')
    try {
      if (!selectedPath) throw new Error('请先选择一个文件夹。')
      await api.post('/api/workspaces', { name: form.get('name'), root_path: selectedPath, description: form.get('description') })
      setPanelOpen(false)
      await workspaces.reload()
    } catch (error) { setFormError(describeError(error)) } finally { setSaving(false) }
  }

  return (
    <div className="page">
      <PageHeader eyebrow="LOCAL SANDBOX" title="工作区" description="把 Agent 的文件访问限制在明确的本地目录内。" action={<button className="button button-primary" onClick={openPanel}><Plus size={16} />新建工作区</button>} />
      <div className="notice"><ShieldCheck size={18} /><p><strong>路径校验已启用</strong> 文件工具会校验工作区边界；命令工具经你审批后在本机执行，请确认命令内容。</p></div>
      {workspaces.error ? <ErrorState message={workspaces.error} onRetry={workspaces.reload} /> : workspaces.loading ? <LoadingState /> : workspaces.data.length ? (
        <section className="entity-grid">
          {workspaces.data.map((workspace) => (
            <article className="entity-card workspace-card" key={workspace.id}>
              <div className="entity-card-top"><div className="folder-icon"><Folder size={21} /></div><span className="menu-dot">•••</span></div>
              <h2>{workspace.name}</h2><p>{workspace.description || '暂无工作区说明'}</p>
              <div className="path-chip" title={workspace.path || workspace.root_path}><span>{workspace.path || workspace.root_path || '路径由 PGAgent 管理'}</span></div>
              <footer><span><Clock3 size={14} />{formatDate(workspace.updated_at || workspace.created_at)}</span><span>安全边界已启用</span></footer>
            </article>
          ))}
        </section>
      ) : <EmptyState icon={Folder} title="先创建一个工作区" description="工作区是 Agent 可以读写文件的安全边界。" action={<button className="button button-primary" onClick={openPanel}><Plus size={16} />新建工作区</button>} />}
      {panelOpen && <SlidePanel title="新建工作区" description="所选目录将成为此工作区唯一允许访问的文件边界。" onClose={() => setPanelOpen(false)}>
        <form className="panel-form" onSubmit={createWorkspace}>
          <Field label="名称"><input name="name" required placeholder="例如：产品调研" autoFocus /></Field>
          <Field label="工作区文件夹" hint="PGAgent 会打开 Windows 文件夹选择器，不需要手动输入路径。">
            <button type="button" className={`folder-picker ${selectedPath ? 'selected' : ''}`} onClick={() => void selectFolder()} disabled={pickingFolder}>
              <span className="folder-picker-icon">{pickingFolder ? <LoaderCircle className="spin" size={21} /> : <FolderOpen size={21} />}</span>
              <span><strong>{selectedPath ? '已选择文件夹' : '选择文件夹'}</strong><small>{selectedPath || '从这台电脑中浏览目录'}</small></span>
              <ChevronRight size={17} />
            </button>
          </Field>
          <Field label="说明"><textarea name="description" rows={4} placeholder="这个工作区用来处理什么？" /></Field>
          {formError && <p className="form-error" role="alert">{formError}</p>}
          <div className="form-actions"><button type="button" className="button button-secondary" onClick={() => setPanelOpen(false)}>取消</button><button className="button button-primary" disabled={saving || !selectedPath}>{saving && <LoaderCircle className="spin" size={15} />}创建</button></div>
        </form>
      </SlidePanel>}
    </div>
  )
}

const builtInTools = ['list_files', 'read_file', 'search_files', 'get_current_time', 'write_file', 'run_command']

function AgentsPage() {
  const agents = useApiData<AgentProfile[]>([], () => api.list<AgentProfile>('/api/agents', ['agents']), [])
  const connections = useApiData<Connection[]>([], () => api.list<Connection>('/api/connections', ['connections']), [])
  const [panelOpen, setPanelOpen] = useState(false)
  const [editing, setEditing] = useState<AgentProfile | null>(null)
  const [saving, setSaving] = useState(false)
  const [deletingId, setDeletingId] = useState('')
  const [formError, setFormError] = useState('')

  function openAgentPanel(agent?: AgentProfile) {
    if (agent?.is_default) return
    setEditing(agent ?? null); setFormError(''); setPanelOpen(true)
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
      <PageHeader eyebrow="REUSABLE INTELLIGENCE" title="Agent" description="配置角色、模型与工具边界，让不同任务使用稳定的工作方式。" action={<button className="button button-primary" onClick={() => openAgentPanel()}><Plus size={16} />创建 Agent</button>} />
      {formError && !panelOpen && <p className="form-error page-form-error" role="alert">{formError}</p>}
      {agents.error ? <ErrorState message={agents.error} onRetry={agents.reload} /> : agents.loading ? <LoadingState /> : agents.data.length ? (
        <section className="entity-grid agent-grid">
          {agents.data.map((agent) => (
            <article className="entity-card agent-card" key={agent.id}>
              <div className="agent-head"><div className="agent-avatar"><Bot size={22} /></div><div className="agent-card-actions"><StatusBadge status={agent.is_default ? 'default' : agent.status || 'idle'} /><button className="icon-button" aria-label={`编辑 ${agent.name}`} disabled={agent.is_default} title={agent.is_default ? '默认助手保持无提示词，不可编辑' : '编辑 Agent'} onClick={() => openAgentPanel(agent)}><Pencil size={15} /></button><button className="icon-button danger-icon" aria-label={`删除 ${agent.name}`} disabled={agent.is_default || deletingId === agent.id} title={agent.is_default ? '默认助手不可删除' : '删除 Agent'} onClick={() => void deleteAgent(agent)}>{deletingId === agent.id ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />}</button></div></div>
              <h2>{agent.name}</h2><p className="agent-role">{agent.is_default ? '无提示词默认助手' : agent.role || '通用执行 Agent'}</p><p>{agent.is_default ? '没有预设系统提示词，直接按照当前会话的目标与模型设置工作。' : agent.description || '暂无角色说明'}</p>
              <div className="agent-meta"><span><Sparkles size={14} />{agent.model || agent.model_id || '继承默认模型'}</span><span><SquareTerminal size={14} />{agent.tools?.length ?? builtInTools.length} 个工具</span></div>
              <footer><span>{formatDate(agent.created_at)}</span><span>{agent.is_default ? '无预设提示词' : `思考：${agent.thinking_level || 'auto'}`}</span></footer>
            </article>
          ))}
        </section>
      ) : <EmptyState icon={Bot} title="创建你的第一个 Agent" description="定义角色、模型和可使用的工具，然后就能在会话中调用它。" action={<button className="button button-primary" onClick={() => openAgentPanel()}><Plus size={16} />创建 Agent</button>} />}
      {panelOpen && <SlidePanel title={editing ? '编辑 Agent' : '创建 Agent'} description="Agent 会根据任务复杂度自行判断是否需要先规划。" onClose={() => { setPanelOpen(false); setEditing(null) }}>
        <form className="panel-form" onSubmit={saveAgent} key={editing?.id || 'new-agent'}>
          <Field label="名称"><input name="name" required placeholder="例如：代码协作者" autoFocus defaultValue={editing?.name || ''} /></Field>
          <Field label="简介"><input name="description" placeholder="简要描述擅长处理的任务" defaultValue={editing?.description || ''} /></Field>
          <Field label="系统指令"><textarea name="system_prompt" rows={6} placeholder="说明工作原则、输出风格和边界……" defaultValue={editing?.system_prompt || ''} /></Field>
          <div className="form-row"><Field label="模型连接"><select name="connection_id" defaultValue={editing?.model_connection_id || editing?.connection_id || ''}><option value="">使用默认连接</option>{connections.data.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field><Field label="模型 ID"><input name="model" placeholder="例如：deepseek-chat" defaultValue={editing?.model_id || editing?.model || ''} /></Field></div>
          <Field label="思考强度"><select name="thinking_level" defaultValue={editing?.thinking_level || 'auto'}><option value="off">关闭</option><option value="auto">自动</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高</option></select></Field>
          <div className="tool-summary"><strong>内置工具</strong><div>{builtInTools.map((tool) => <span key={tool}>{tool}</span>)}</div><small>write_file 与 run_command 默认需要人工审批。</small></div>
          {formError && <p className="form-error" role="alert">{formError}</p>}
          <div className="form-actions"><button type="button" className="button button-secondary" onClick={() => { setPanelOpen(false); setEditing(null) }}>取消</button><button className="button button-primary" disabled={saving}>{saving && <LoaderCircle className="spin" size={15} />}{editing ? '保存修改' : '创建'}</button></div>
        </form>
      </SlidePanel>}
    </div>
  )
}

function SessionsPage() {
  const sessions = useApiData<Session[]>([], () => api.list<Session>('/api/sessions', ['sessions']), [])
  const agents = useApiData<AgentProfile[]>([], () => api.list<AgentProfile>('/api/agents', ['agents']), [])
  const workspaces = useApiData<Workspace[]>([], () => api.list<Workspace>('/api/workspaces', ['workspaces']), [])
  const connections = useApiData<Connection[]>([], () => api.list<Connection>('/api/connections', ['connections']), [])
  const [activeId, setActiveId] = useState('')
  const [sessionQuery, setSessionQuery] = useState('')
  const [composer, setComposer] = useState('')
  const [sending, setSending] = useState(false)
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [settingsMenuOpen, setSettingsMenuOpen] = useState(false)
  const [settingsSubmenu, setSettingsSubmenu] = useState<'model' | 'thinking' | null>(null)
  const settingsMenuRef = useRef<HTMLDivElement>(null)
  const settingsTriggerRef = useRef<HTMLButtonElement>(null)
  const modelSubmenuRef = useRef<HTMLDivElement>(null)
  const thinkingSubmenuRef = useRef<HTMLDivElement>(null)
  const [decidingApproval, setDecidingApproval] = useState('')
  const [actionError, setActionError] = useState('')
  const [newSession, setNewSession] = useState(false)
  const [liveRun, setLiveRun] = useState<LiveRunState>({ runId: '', phase: '', draft: '', status: 'idle', error: '' })
  const eventSourceRef = useRef<EventSource | null>(null)
  const fallbackTimerRef = useRef<number | null>(null)
  const streamReconnectTimerRef = useRef<number | null>(null)
  const streamErrorCountRef = useRef(0)
  const streamRunIdRef = useRef('')
  const seenStreamEventsRef = useRef<{ runId: string; eventIds: Set<string> }>({ runId: '', eventIds: new Set() })
  const runStartMessageCountRef = useRef(0)
  const messagesRef = useRef<HTMLDivElement>(null)
  const activeIdRef = useRef('')
  const activeRunIdRef = useRef('')
  const stickToBottomRef = useRef(true)
  const terminalSyncVersionRef = useRef(0)
  activeIdRef.current = activeId
  const liveRunRef = useRef(liveRun)
  liveRunRef.current = liveRun

  useEffect(() => {
    if (!activeId && sessions.data[0]) setActiveId(stringId(sessions.data[0].id))
  }, [activeId, sessions.data])

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
    setSettingsMenuOpen(false); setSettingsSubmenu(null)
  }, [activeId])

  const messages = useApiData<OwnedSessionMessages>(
    { ownerSessionId: '', items: [] },
    async () => activeId
      ? { ownerSessionId: activeId, items: await api.list<Message>(`/api/sessions/${activeId}/messages`, ['messages']) }
      : { ownerSessionId: '', items: [] },
    [activeId],
  )
  const runs = useApiData<Run[]>([], () => activeId ? api.list<Run>(`/api/runs?session_id=${encodeURIComponent(activeId)}`, ['runs']) : Promise.resolve([]), [activeId])
  const context = useApiData<SessionContext | null>(null, () => activeId ? api.get<SessionContext>(`/api/sessions/${activeId}/context`) : Promise.resolve(null), [activeId])
  const activeSession = sessions.data.find((item) => stringId(item.id) === activeId)
  const activeAgent = agents.data.find((item) => item.id === activeSession?.agent_id)
  const sessionRuns = runs.data.filter((item) => !item.session_id || item.session_id === activeId)
  const activeRun = sessionRuns.find((item) => activeRunStatuses.has(item.status || '')) ?? sessionRuns[0]
  const activeRunId = activeRun?.id || ''
  activeRunIdRef.current = activeRunId
  const visibleMessages = visibleSessionItems(messages.data.ownerSessionId, activeId, messages.data.items)
  const approvals = useApiData<Approval[]>([], () => activeRunId ? api.list<Approval>(`/api/approvals?run_id=${encodeURIComponent(activeRunId)}&status=pending`, ['approvals']) : Promise.resolve([]), [activeRunId])
  const visibleApprovals = approvals.data.filter((approval) => !approval.run_id || approval.run_id === activeRunId)
  const refreshMessages = messages.refresh
  const refreshRuns = runs.refresh
  const refreshContext = context.refresh
  const setMessagesState = messages.setState
  const setApprovalsState = approvals.setState
  const settingsLocked = activeRunStatuses.has(activeRun?.status || '') || !['idle', 'terminal'].includes(liveRun.status)
  const visibleSessions = sessions.data.filter((session) => (session.title || '').toLowerCase().includes(sessionQuery.trim().toLowerCase()))
  useEffect(() => {
    if (settingsLocked || sending) {
      setSettingsMenuOpen(false)
      setSettingsSubmenu(null)
    }
  }, [sending, settingsLocked])

  useEffect(() => {
    setMessagesState({ data: { ownerSessionId: activeId, items: [] }, loading: true, error: '' })
  }, [activeId, setMessagesState])

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

  const sessionRunStillCurrent = useCallback((sessionId: string, runId: string) => {
    return isCurrentSessionRun(activeIdRef.current, activeRunIdRef.current, streamRunIdRef.current, sessionId, runId)
  }, [])

  const refreshApprovalsForRun = useCallback(async (runId: string, sessionId: string) => {
    if (!sessionRunStillCurrent(sessionId, runId)) return undefined
    try {
      const data = await api.list<Approval>(`/api/approvals?run_id=${encodeURIComponent(runId)}&status=pending`, ['approvals'])
      if (!sessionRunStillCurrent(sessionId, runId)) return undefined
      setApprovalsState({ data, loading: false, error: '' })
      return data
    } catch {
      return undefined
    }
  }, [sessionRunStillCurrent, setApprovalsState])

  const syncTerminalRun = useCallback(async (runId: string, event?: RunStreamEvent, sessionId = activeIdRef.current) => {
    const syncVersion = ++terminalSyncVersionRef.current
    closeRunTransport()
    setLiveRun((previous) => ({
      ...previous,
      runId,
      phase: event ? runStreamPhase(event) : previous.phase,
      status: 'terminal',
      error: event?.error ? String(event.error) : previous.error,
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

    await Promise.all([refreshRuns(), refreshContext(), refreshApprovalsForRun(runId, sessionId)])
    if (syncVersion !== terminalSyncVersionRef.current || activeIdRef.current !== sessionId) return

    const assistantCount = refreshedMessages?.ownerSessionId === sessionId
      ? refreshedMessages.items.filter((message) => message.role === 'assistant').length
      : 0
    const hasPersistedReply = assistantCount > runStartMessageCountRef.current
    const hasDraft = Boolean(liveRunRef.current.runId === runId && liveRunRef.current.draft)
    if (event?.error) setActionError(`运行失败：${String(event.error)}`)
    if (hasPersistedReply || !hasDraft) {
      setLiveRun({ runId: '', phase: '', draft: '', status: 'idle', error: '' })
    }
  }, [closeRunTransport, refreshApprovalsForRun, refreshContext, refreshMessages, refreshRuns])

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
        if (status === 'awaiting_approval') void refreshApprovalsForRun(runId, sessionId)
        if (isTerminalRunStatus(status)) {
          await syncTerminalRun(runId, { type: 'run_state', status, terminal: true, error: status === 'failed' ? run.stop_reason : undefined, reason: run.stop_reason }, sessionId)
        }
      } finally {
        polling = false
      }
    }

    void poll()
    fallbackTimerRef.current = window.setInterval(() => void poll(), 4000)
  }, [refreshApprovalsForRun, refreshRuns, syncTerminalRun])

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
      const waitingApproval = parsed.type === 'approval_requested' || (parsed.type === 'run_state' && parsed.status === 'awaiting_approval')
      setLiveRun((previous) => ({
        ...previous,
        runId,
        phase: runStreamPhase(parsed),
        draft: appendAssistantDelta(previous.draft, parsed),
        status: terminal ? 'terminal' : waitingApproval ? 'awaiting_approval' : 'live',
        error: parsed.error ? String(parsed.error) : previous.error,
      }))
      if (waitingApproval) void refreshApprovalsForRun(runId, sessionId)
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
  }, [refreshApprovalsForRun, startRunFallback, syncTerminalRun])

  useEffect(() => {
    closeRunTransport()
    terminalSyncVersionRef.current += 1
    seenStreamEventsRef.current = { runId: '', eventIds: new Set() }
    setLiveRun({ runId: '', phase: '', draft: '', status: 'idle', error: '' })
    stickToBottomRef.current = true
    return () => {
      closeRunTransport()
      terminalSyncVersionRef.current += 1
    }
  }, [activeId, closeRunTransport])

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
    if (!activeId || !composer.trim() || settingsSaving || settingsLocked) return
    setSending(true); setActionError('')
    stickToBottomRef.current = true
    runStartMessageCountRef.current = visibleMessages.filter((message) => message.role === 'assistant').length
    setLiveRun({ runId: '', phase: '思考中…', draft: '', status: 'connecting', error: '' })
    try {
      const launched = await api.post<Run>(`/api/sessions/${activeId}/run`, { content: composer.trim() })
      setComposer('')
      void messages.refresh()
      void runs.refresh()
      void context.refresh()
      startRunStream(launched.id, activeId)
    } catch (error) {
      setLiveRun({ runId: '', phase: '', draft: '', status: 'idle', error: '' })
      setActionError(describeError(error))
    } finally { setSending(false) }
  }

  async function createSession(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setSending(true); setActionError('')
    try {
      const created = await api.post<Session>('/api/sessions', { title: form.get('title'), agent_id: form.get('agent_id') || undefined, workspace_id: form.get('workspace_id') || undefined })
      setNewSession(false); await sessions.reload(); setActiveId(stringId(created.id))
    } catch (error) { setActionError(describeError(error)) } finally { setSending(false) }
  }

  async function decideApproval(id: string, decision: 'approve' | 'reject') {
    const approvalSessionId = activeIdRef.current
    const approvalRunId = activeRunIdRef.current || streamRunIdRef.current
    if (!sessionRunStillCurrent(approvalSessionId, approvalRunId)) return
    setActionError(''); setDecidingApproval(id)
    try {
      await api.post(`/api/approvals/${id}/decide`, { decision })
      if (!sessionRunStillCurrent(approvalSessionId, approvalRunId)) return
      await Promise.all([refreshApprovalsForRun(approvalRunId, approvalSessionId), refreshRuns()])
      if (!sessionRunStillCurrent(approvalSessionId, approvalRunId)) return
      setLiveRun((previous) => {
        if (!sessionRunStillCurrent(approvalSessionId, approvalRunId) || !shouldMarkApprovalResuming(previous.status, previous.runId, approvalRunId)) return previous
        return { ...previous, phase: decision === 'approve' ? '审批已通过，继续处理…' : '正在停止运行…', status: 'connecting', error: '' }
      })
    } catch (error) {
      if (sessionRunStillCurrent(approvalSessionId, approvalRunId)) setActionError(describeError(error))
    } finally { setDecidingApproval('') }
  }

  async function updateSessionSettings(payload: { model_connection_id?: string | null; model_id?: string | null; thinking_level?: ThinkingLevel | null }) {
    if (!activeId || settingsLocked || sending) return
    setSettingsSaving(true); setActionError('')
    try { await api.patch(`/api/sessions/${activeId}`, payload); await sessions.reload() }
    catch (error) { setActionError(describeError(error)) } finally { setSettingsSaving(false) }
  }

  const effectiveSettings = resolveEffectiveModelSettings(connections.data, activeSession, activeAgent)
  const effectiveConnection = effectiveSettings.connection
  const effectiveModel = effectiveSettings.model
  const automaticSessionConnection = effectiveSettings.automaticConnection
  const automaticModel = effectiveSettings.automaticModel
  const effectiveThinking = resolveEffectiveThinking(activeSession?.thinking_level, activeAgent?.thinking_level, effectiveConnection?.thinking_level)
  const modelOptions = connections.data.filter((connection) => connection.enabled !== false).flatMap((connection) => {
    return availableConnectionModels(connection).map((model) => ({ value: `${connection.id}::${model}`, label: model, connection: connection.name }))
  })
  const selectedModelValue = effectiveSettings.selectedValue
  const selectedThinkingValue = activeSession?.thinking_level && activeSession.thinking_level !== 'auto' ? activeSession.thinking_level : 'auto'
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
          <aside className="session-list">
            <button className="session-create" onClick={() => { setActionError(''); setNewSession(true) }}><Plus size={15} />新建对话</button>
            <div className="session-search"><Search size={15} /><input aria-label="搜索会话" placeholder="搜索会话" value={sessionQuery} onChange={(event) => setSessionQuery(event.target.value)} /></div>
            {sessions.error && <div className="session-list-error"><AlertCircle size={14} /><span>{sessions.error}</span><button type="button" onClick={() => void sessions.reload()}>重试</button></div>}
            {!!dependencyErrors.length && <div className="session-list-error"><AlertCircle size={14} /><span>{dependencyErrors.join('；')}</span><button type="button" onClick={() => { void Promise.all([agents.reload(), workspaces.reload(), connections.reload()]) }}>重试</button></div>}
            {sessions.loading && !sessions.data.length ? <LoadingState /> : visibleSessions.length ? visibleSessions.map((session) => (
              <button key={session.id} className={`session-item ${activeId === stringId(session.id) ? 'active' : ''}`} onClick={() => setActiveId(stringId(session.id))}>
                <MessageSquare size={17} /><span><strong>{session.title || '未命名会话'}</strong><small>{formatDate(session.updated_at || session.created_at)}</small></span>
              </button>
            )) : <div className="mini-empty"><MessageSquare size={20} /><p>{sessions.data.length ? '没有匹配的会话' : '还没有会话'}</p></div>}
          </aside>
          <section className="conversation">
            {activeSession ? <>
              <div
                className="messages"
                ref={messagesRef}
                aria-live="polite"
                onScroll={(event) => {
                  const element = event.currentTarget
                  stickToBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80
                }}
              >
                {messages.error && !visibleMessages.length ? <ErrorState message={messages.error} onRetry={messages.reload} /> : messages.loading && !visibleMessages.length ? <LoadingState /> : visibleMessages.length ? visibleMessages.map((message) => <MessageBubble key={message.id} message={message} />) : liveRun.status === 'idle' ? <EmptyState icon={MessageSquare} title="从一条清晰的任务开始" description="描述目标、约束和期望产物，Agent 会先理解上下文再行动。" /> : null}
                {messages.error && !!visibleMessages.length && <p className="inline-error" role="alert">消息同步失败：{messages.error}</p>}
                {liveRun.status !== 'idle' && <LiveAssistantMessage liveRun={liveRun} />}
                {approvals.error && <ErrorState message={`审批状态读取失败：${approvals.error}`} onRetry={approvals.reload} />}
                {approvals.loading && activeRunId && !visibleApprovals.length && liveRun.status === 'awaiting_approval' && <LoadingState label="正在读取审批状态" />}
                {visibleApprovals.map((approval) => <ApprovalCard key={approval.id} approval={approval} deciding={decidingApproval === approval.id} onDecision={decideApproval} />)}
              </div>
              <form className="composer" onSubmit={sendMessage}>
                {actionError && <p className="form-error" role="alert">{actionError}</p>}
                <textarea aria-label="给 Agent 发送消息" value={composer} onChange={(event) => setComposer(event.target.value)} placeholder="告诉 PGAgent 你想完成什么……" rows={3} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} />
                <div className="composer-toolbar">
                  <div className="composer-actions">
                    {context.error ? <button type="button" className="context-state context-error" aria-label="上下文占用读取失败，点击重试" title={context.error} onClick={() => void context.reload()}><AlertCircle size={15} /></button> : context.loading || !context.data ? <span className="context-state" role="status" aria-label="正在读取上下文占用"><LoaderCircle className="spin" size={15} /></span> : <ContextUsageRing context={context.data} />}
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
                    <button className="send-button" aria-label="发送" disabled={sending || settingsSaving || settingsLocked || !composer.trim()}>{sending ? <LoaderCircle className="spin" /> : <Send />}</button>
                  </div>
                </div>
              </form>
            </> : <EmptyState icon={MessageSquare} title="选择或创建会话" description="不选择 Agent 时会自动使用无预设提示词的默认助手。" action={<button className="button button-primary" onClick={() => setNewSession(true)}>新建会话</button>} />}
          </section>
      </div>
      {newSession && <SlidePanel title="新建会话" description="Agent 与工作区都可以留空，由 PGAgent 自动选择默认项。" onClose={() => setNewSession(false)}>
        <form className="panel-form" onSubmit={createSession}>
          <Field label="会话名称"><input name="title" required placeholder="例如：整理本周需求" autoFocus /></Field>
          <Field label="Agent" hint="不选择时使用没有预设提示词的默认助手。"><select name="agent_id" defaultValue=""><option value="">自动（默认助手）</option>{agents.data.filter((agent) => !agent.is_default).map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></Field>
          <Field label="工作区" hint="不选择时使用 PGAgent 的默认安全工作区。"><select name="workspace_id" defaultValue=""><option value="">自动（默认工作区）</option>{workspaces.data.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}</select></Field>
          {actionError && <p className="form-error" role="alert">{actionError}</p>}
          <div className="form-actions"><button type="button" className="button button-secondary" onClick={() => setNewSession(false)}>取消</button><button className="button button-primary" disabled={sending}>{sending && <LoaderCircle className="spin" size={15} />}创建</button></div>
        </form>
      </SlidePanel>}
    </div>
  )
}

function MessageBubble({ message }: { message: Message }) {
  const isTool = message.role === 'tool' || !!message.tool_name
  return (
    <article className={`message ${message.role} ${isTool ? 'tool-message' : ''}`}>
      <div className="message-avatar">{message.role === 'user' ? '你' : isTool ? <SquareTerminal size={16} /> : <Sparkles size={16} />}</div>
      <div className="message-body"><div className="message-meta"><strong>{message.role === 'user' ? '你' : isTool ? message.tool_name || '工具结果' : 'PGAgent'}</strong><time>{formatDate(message.created_at)}</time></div><div className="message-content">{message.content}</div>{message.status && <StatusBadge status={message.status} />}</div>
    </article>
  )
}

function LiveAssistantMessage({ liveRun }: { liveRun: LiveRunState }) {
  return (
    <article className={`message assistant live-message ${liveRun.status === 'terminal' ? 'live-message-terminal' : ''}`}>
      <div className="message-avatar"><Sparkles size={16} /></div>
      <div className="message-body">
        <div className="message-meta"><strong>PGAgent</strong><span className="live-phase"><i aria-hidden="true" />{liveRun.phase || '思考中…'}</span></div>
        {liveRun.draft && <div className="message-content">{liveRun.draft}</div>}
        {liveRun.error && <p className="live-error">{liveRun.error}</p>}
      </div>
    </article>
  )
}

function ApprovalCard({ approval, deciding, onDecision }: { approval: Approval; deciding: boolean; onDecision: (id: string, decision: 'approve' | 'reject') => void }) {
  return (
    <article className="approval-card">
      <header><span><ShieldCheck size={17} /></span><div><strong>需要你的批准</strong><p>Agent 请求执行有副作用的工具</p></div><StatusBadge status={approval.status || 'pending'} /></header>
      <div className="approval-command"><span>{approval.tool_name || 'unknown_tool'}</span><pre>{JSON.stringify(approval.arguments ?? {}, null, 2)}</pre></div>
      {approval.reason && <p className="approval-reason">理由：{approval.reason}</p>}
      <footer><button className="button button-danger" disabled={deciding} onClick={() => onDecision(approval.id, 'reject')}><XCircle size={15} />拒绝</button><button className="button button-primary" disabled={deciding} onClick={() => onDecision(approval.id, 'approve')}>{deciding ? <LoaderCircle className="spin" size={15} /> : <CheckCircle2 size={15} />}允许本次</button></footer>
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

function UsagePage() {
  const emptySummary: UsageSummary = { total_requests: 0, input_tokens: 0, output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0, total_tokens: 0, total_cost_usd: 0, cache_hit_rate: 0 }
  const summary = useApiData<UsageSummary>(emptySummary, () => api.get<UsageSummary>('/api/usage/summary'), [])
  const models = useApiData<ModelUsage[]>([], () => api.list<ModelUsage>('/api/usage/models'), [])
  const cacheRate = Math.min(100, Math.max(0, summary.data.cache_hit_rate <= 1 ? summary.data.cache_hit_rate * 100 : summary.data.cache_hit_rate))
  const usageCards = [
    { label: '输入 Token', value: formatTokens(summary.data.input_tokens), icon: ArrowRight },
    { label: '输出 Token', value: formatTokens(summary.data.output_tokens), icon: Sparkles },
    { label: '缓存创建', value: formatTokens(summary.data.cache_creation_tokens), icon: Database },
    { label: '缓存命中', value: formatTokens(summary.data.cache_read_tokens), icon: CheckCircle2 },
  ]
  return (
    <div className="page usage-page">
      <PageHeader eyebrow="TOKEN & COST" title="用量统计" description="集中查看所有模型请求的 Token、缓存与成本；无法获得价格的模型按 $0 显示。" action={<button className="button button-secondary" onClick={() => void Promise.all([summary.reload(), models.reload()])}><RefreshCw size={15} />刷新</button>} />
      {summary.error ? <ErrorState message={summary.error} onRetry={summary.reload} /> : summary.loading ? <LoadingState /> : <>
        <section className="usage-hero card">
          <div className="usage-total"><span className="usage-bolt"><ChartNoAxesCombined size={22} /></span><div><p>实际消耗 Token</p><strong>{summary.data.total_tokens.toLocaleString()}</strong><small>≈ {formatTokens(summary.data.total_tokens)}</small></div></div>
          <dl className="usage-mini-totals"><div><dt>成功模型响应数</dt><dd>{summary.data.total_requests.toLocaleString()}</dd></div><div><dt>总成本</dt><dd>{formatCost(summary.data.total_cost_usd)}</dd></div></dl>
          <div className="usage-card-grid">{usageCards.map(({ label, value, icon: Icon }) => <article key={label}><span><Icon size={15} />{label}</span><strong>{value}</strong></article>)}</div>
          <div className="cache-meter"><div><span>缓存命中率</span><strong>{cacheRate.toFixed(1)}%</strong></div><i><b style={{ width: `${cacheRate}%` }} /></i></div>
        </section>
      </>}
      <section className="usage-models card">
        <div className="card-heading"><div><p className="eyebrow">BY MODEL</p><h2>模型明细</h2></div></div>
        {models.error ? <ErrorState message={models.error} onRetry={models.reload} /> : models.loading ? <LoadingState /> : models.data.length ? <div className="usage-table-wrap"><table><thead><tr><th>模型 / 提供商</th><th>响应数</th><th>Tokens</th><th>总成本</th><th>平均成本</th></tr></thead><tbody>{models.data.map((model) => <tr key={`${model.provider || 'unknown'}:${model.model_id}`}><th><span>{model.model_id || 'unknown'}</span><small>{model.provider || 'unknown provider'}</small></th><td>{model.requests.toLocaleString()}</td><td>{model.tokens.toLocaleString()}</td><td>{formatCost(model.total_cost_usd)}</td><td>{formatCost(model.avg_cost_usd)}</td></tr>)}</tbody></table></div> : <EmptyState icon={ChartNoAxesCombined} title="还没有用量记录" description="完成一次模型调用后，这里会按提供商与模型累计 Token 和成本。" />}
      </section>
    </div>
  )
}

function TeamsPage() {
  const tasks = useApiData<TeamTask[]>([], () => api.list<TeamTask>('/api/teams/tasks', ['tasks']), [])
  const agents = useApiData<AgentProfile[]>([], () => api.list<AgentProfile>('/api/agents', ['agents']), [])
  const [panelOpen, setPanelOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')

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
      {panelOpen && <SlidePanel title="创建团队任务" description="任务将写入持久化任务板，供 Lead 或 Worker 认领。" onClose={() => setPanelOpen(false)}><form className="panel-form" onSubmit={createTask}><Field label="任务标题"><input name="title" required placeholder="清晰描述可独立交付的子任务" autoFocus /></Field><Field label="任务说明"><textarea name="description" rows={5} placeholder="包含输入、约束和验收标准" /></Field><div className="form-row"><Field label="优先级"><select name="priority" defaultValue="normal"><option value="low">低</option><option value="normal">普通</option><option value="high">高</option></select></Field><Field label="指派 Agent"><select name="assignee_agent_id"><option value="">暂不指派</option>{agents.data.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></Field></div>{formError && <p className="form-error">{formError}</p>}<div className="form-actions"><button type="button" className="button button-secondary" onClick={() => setPanelOpen(false)}>取消</button><button className="button button-primary" disabled={saving}>{saving && <LoaderCircle className="spin" size={15} />}创建</button></div></form></SlidePanel>}
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
