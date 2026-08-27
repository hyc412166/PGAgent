import { Activity, AlertCircle, Bot, Database, History, LoaderCircle, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { api } from '../../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, SlidePanel, StatusBadge } from '../../components/ui'
import { statusText } from '../../components/status'
import { useApiData } from '../../shared/hooks/useApiData'
import { formatCost, formatDate, formatTokens } from '../../shared/lib/display'
import type { Run, RunUsage } from '../../types'

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

function RunsPage() {
  const runs = useApiData<Run[]>([], () => api.list<Run>('/api/runs', ['runs']), [])
  const [selected, setSelected] = useState<Run | null>(null)
  const [filter, setFilter] = useState('all')
  const filtered = filter === 'all' ? runs.data : runs.data.filter((run) => run.status === filter)
  return (
    <div className="page">
      <PageHeader eyebrow="行动足迹" title="运行记录" description="每一次计划、工具调用、重试和停止原因都保留可追溯记录。" action={<button className="button button-secondary" onClick={runs.reload}><RefreshCw size={15} />刷新</button>} />
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
  const usage = useApiData<RunUsage | null>(null, () => api.get<RunUsage | null>(`/api/usage/runs/${run.id}`), [run.id])
  const timeline = run.events?.length ? run.events : events.data
  return (
    <div className="run-detail">
      <div className="detail-hero"><StatusBadge status={run.status} /><strong>{statusText[run.phase || run.status || ''] ?? run.phase ?? run.status ?? '暂无阶段信息'}</strong></div>
      <dl className="detail-grid"><div><dt>执行步数</dt><dd>{run.step_count ?? run.current_step ?? 0}</dd></div><div><dt>工具调用</dt><dd>{run.tool_call_count ?? run.tool_calls ?? 0}</dd></div></dl>
      {usage.data ? <>
        <div className="run-usage-heading"><Database size={14} /><strong>本次用量</strong><span>{usage.data.provider || '未知提供商'} · {usage.data.model_id || '未知模型'}</span></div>
        <dl className="detail-grid run-usage-grid"><div><dt>输入 Token</dt><dd>{formatTokens(usage.data.input_tokens)}</dd></div><div><dt>输出 Token</dt><dd>{formatTokens(usage.data.output_tokens)}</dd></div><div><dt>缓存命中</dt><dd>{(usage.data.cache_hit_rate * 100).toFixed(1)}%</dd></div><div><dt>成本</dt><dd>{formatCost(usage.data.total_cost_usd)}</dd></div></dl>
      </> : usage.initialLoading ? <p className="run-usage-note"><LoaderCircle className="spin" size={14} />正在读取本次 Token 用量…</p> : <p className="run-usage-note"><Database size={14} />本次运行没有返回可统计的 Token 明细。</p>}
      {run.stop_reason && <div className="stop-reason"><AlertCircle size={17} /><div><strong>停止原因</strong><p>{run.stop_reason}</p></div></div>}
      <h3>事件时间线</h3>
      {events.error ? <ErrorState message={events.error} onRetry={events.reload} /> : events.loading ? <LoadingState /> : timeline?.length ? <div className="timeline">{timeline.map((event, index) => <div key={event.id || index}><span /><div><strong>{event.message || event.type || event.event_type || event.phase || '运行事件'}</strong><small>{formatDate(event.created_at)}</small></div></div>)}</div> : <EmptyState icon={Activity} title="暂无事件详情" description="后端记录运行事件后会在这里展示。" />}
    </div>
  )
}

export { RunRow, RunsPage }
