import { Activity, ArrowRight, Bot, CheckCircle2, ChevronRight, Folder, Play, ShieldCheck, SquareTerminal, Workflow } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../api'
import { EmptyState, ErrorState, LoadingState, PageHeader } from '../../components/ui'
import { RunRow } from '../runs/RunsPage'
import { useApiData } from '../../shared/hooks/useApiData'
import { formatDate } from '../../shared/lib/display'
import type { Approval, DashboardData, Run } from '../../types'

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
      <PageHeader eyebrow="今日冰面" title="工作台总览" description="Agent 状态、最近运行和待确认事项，都在这里一眼看清。" action={<button className="button button-primary" onClick={() => navigate('/sessions')}><Play size={16} />开始新任务</button>} />
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
          <div className="card-heading"><div><p className="eyebrow">最近足迹</p><h2>最近运行</h2></div><button className="text-button" onClick={() => navigate('/runs')}>查看全部<ArrowRight size={15} /></button></div>
          {recentRuns.error ? <ErrorState message={recentRuns.error} onRetry={recentRuns.reload} /> : dashboard.loading || recentRuns.loading ? <LoadingState /> : displayedRuns.length ? (
            <div className="run-list">
              {displayedRuns.slice(0, 5).map((run) => <RunRow key={run.id} run={run} onClick={() => navigate('/runs')} />)}
            </div>
          ) : <EmptyState icon={Workflow} title="还没有运行记录" description="创建工作区和 Agent 后，发起第一条任务。" action={<button className="button button-secondary" onClick={() => navigate('/sessions')}>前往会话</button>} />}
        </section>
        <aside className="card attention-card">
          <div className="card-heading"><div><p className="eyebrow">待确认</p><h2>等待你处理</h2></div><span className="number-chip">{approvals.data.length}</span></div>
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


export { DashboardPage }
