// 本文件实现 RunsPage 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
import { Activity, AlertCircle, Bot, Database, History, LoaderCircle, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { api, describeError } from '../../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, SlidePanel, StatusBadge } from '../../components/ui'
import { statusText } from '../../components/status'
import { useApiData } from '../../shared/hooks/useApiData'
import { formatCost, formatTokens } from '../../shared/lib/display'
import type { Run, RunEventFilters, RunEventPage, RunUsage } from '../../types'
import { formatRunEventOffset, formatRunEventTime, isHiddenRunEvent, presentRunEvent, runDisplayTitle, runSecondaryLabel } from '../../runEventPresentation'
import { appendRunEventPage } from './runEventPagination'

// RunRow 将单条运行压缩为可选择的列表行，状态、时间和目标信息均来自 Run。
function RunRow({ run, onClick }: { run: Run; onClick?: () => void }) {
  return (
    <button className="run-row" onClick={onClick} disabled={!onClick}>
      <div className="run-avatar"><Bot size={17} /></div>
      <div className="run-main"><strong title={runDisplayTitle(run)}>{runDisplayTitle(run)}</strong><span>{runSecondaryLabel(run)}</span></div>
      <StatusBadge status={run.status} />
      <span className="run-metrics">{run.step_count ?? run.current_step ?? 0} 步 · {run.tool_call_count ?? run.tool_calls ?? 0} 次工具</span>
      <time>{formatRunEventTime(run.updated_at || run.finished_at || run.created_at || run.started_at)}</time>
    </button>
  )
}

// RunsPage 读取运行历史并按状态筛选；选择记录后由 RunDetails 展开事件和用量。
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
      {selected && <SlidePanel title={runDisplayTitle(selected)} description={runSecondaryLabel(selected)} onClose={() => setSelected(null)}><RunDetails run={selected} /></SlidePanel>}
    </div>
  )
}

const initialRunEventFilters: RunEventFilters = { event_type: '', step: undefined, errors_only: false, limit: 50 }

// RunEventFilterControls 只收集后端支持的诊断筛选，不在浏览器重做事件语义判断。
function RunEventFilterControls({ filters, onChange }: { filters: RunEventFilters; onChange: (filters: RunEventFilters) => void }) {
  return <div className="filter-bar" role="group" aria-label="运行事件筛选">
    <label>事件类型<input value={filters.event_type || ''} placeholder="例如 model_retry" onChange={(event) => onChange({ ...filters, event_type: event.target.value, before: undefined })} /></label>
    <label>步骤<input type="number" min={0} value={filters.step ?? ''} placeholder="全部" onChange={(event) => onChange({ ...filters, step: event.target.value === '' ? undefined : Number(event.target.value), before: undefined })} /></label>
    <label><input type="checkbox" checked={filters.errors_only || false} onChange={(event) => onChange({ ...filters, errors_only: event.target.checked, before: undefined })} />仅看错误</label>
  </div>
}

// RunDetails 以 run.id 并行加载事件时间线与计费用量，形成运行诊断详情。
function RunDetails({ run }: { run: Run }) {
  const [filters, setFilters] = useState<RunEventFilters>(initialRunEventFilters)
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadMoreError, setLoadMoreError] = useState('')
  const events = useApiData<RunEventPage>({ items: [], next_before: null }, () => api.listRunEvents(run.id, filters), [run.id, filters.event_type, filters.step, filters.errors_only, filters.limit])
  const usage = useApiData<RunUsage | null>(null, () => api.get<RunUsage | null>(`/api/usage/runs/${run.id}`), [run.id])
  const timeline = events.data.items
  const timelineStartedAt = run.started_at || timeline.at(-1)?.created_at
  async function loadMore() {
    if (events.data.next_before === null || loadingMore) return
    setLoadingMore(true)
    setLoadMoreError('')
    try {
      const incoming = await api.listRunEvents(run.id, { ...filters, before: events.data.next_before })
      events.setState({ data: appendRunEventPage(events.data, incoming), loading: false, error: '' })
    } catch (error) {
      setLoadMoreError(describeError(error))
    } finally {
      setLoadingMore(false)
    }
  }
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
      <RunEventFilterControls filters={filters} onChange={setFilters} />
      {events.error ? <ErrorState message={events.error} onRetry={events.reload} /> : events.loading ? <LoadingState /> : timeline?.filter((event) => !isHiddenRunEvent(event)).length ? <div className="timeline run-event-timeline">{timeline.filter((event) => !isHiddenRunEvent(event)).map((event, index) => {
        const presented = presentRunEvent(event)
        const type = event.type || event.event_type || 'runtime_event'
        return <div className={`run-event tone-${presented.tone}`} key={event.id || index}>
          <span className="run-event-dot" />
          <article>
            <header><strong>{presented.title}</strong><time>{formatRunEventTime(event.created_at)}</time></header>
            <p>{presented.detail}</p>
            {!!presented.facts.length && <dl>{presented.facts.map((fact, factIndex) => <div key={`${fact.label}:${factIndex}`}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}</dl>}
            <footer><code>{type}</code>{event.step != null && <span>第 {event.step} 步</span>}<span>{formatRunEventOffset(event.created_at, timelineStartedAt) || '+0 毫秒'}</span></footer>
          </article>
        </div>
      })}</div> : <EmptyState icon={Activity} title="暂无事件详情" description="后端记录运行事件后会在这里展示。" />}
      {loadMoreError && <p className="inline-error">{loadMoreError}</p>}
      {events.data.next_before !== null && <button type="button" className="button button-secondary" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? <><LoaderCircle className="spin" size={14} />正在加载…</> : '加载更多'}</button>}
    </div>
  )
}

export { RunEventFilterControls, RunRow, RunsPage }
