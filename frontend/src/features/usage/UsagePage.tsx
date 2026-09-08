// 本文件实现 UsagePage 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
import { ArrowRight, CalendarDays, ChartNoAxesCombined, CheckCircle2, Database, LoaderCircle, RefreshCw, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { api } from '../../api'
import { EmptyState, ErrorState, LoadingState, PageHeader } from '../../components/ui'
import { useApiData } from '../../shared/hooks/useApiData'
import { formatCost, formatTokens, usageRate } from '../../shared/lib/display'
import { usageDateKey, usageDateOptions, usageDatePresetBounds, usageDateRange, usageRangeLabel } from '../../usageDateRange'
import type { QuickUsageDatePreset, UsageDatePreset } from '../../usageDateRange'
import type { ModelUsage, UsageBreakdownItem, UsageSession, UsageSummary, UsageWorkspace } from '../../types'

// UsagePage 根据统一日期范围并行读取总量、模型、会话和项目四个维度的用量。
function UsagePage() {
  const emptySummary: UsageSummary = { total_requests: 0, input_tokens: 0, output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0, total_tokens: 0, total_cost_usd: 0, cache_hit_rate: 0 }
  const today = usageDateKey(new Date())
  // start/end 是实际查询边界，datePreset/rangeAnchor 保存快捷范围语义及其计算基准。
  const [startDate, setStartDate] = useState(today)
  const [endDate, setEndDate] = useState(today)
  const [datePreset, setDatePreset] = useState<UsageDatePreset>('today')
  const [rangeAnchor, setRangeAnchor] = useState(() => new Date())
  const range = usageDateRange(startDate, endDate, datePreset, rangeAnchor)
  // 四个资源共享 range，因此日期变化后会同步重新加载，避免维度间时间窗口不一致。
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
      <PageHeader eyebrow="能量账本" title="用量统计" description="按时间范围查看模型、对话与项目的 Token、缓存和成本。" action={<button className="button button-secondary" onClick={reloadUsage}><RefreshCw size={15} />刷新</button>} />
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

// UsageRefreshBadge 为后台更新状态提供一致的轻量视觉标记。
function UsageRefreshBadge() {
  return <span className="usage-refresh-badge" role="status"><LoaderCircle className="spin" size={12} />更新中</span>
}

// UsageBreakdown 复用模型/会话/项目排行结构，并统一处理首次加载、刷新、错误和空数据。
function UsageBreakdown({ title, eyebrow, emptyDescription, data, initialLoading, refreshing, error, onRetry }: { title: string; eyebrow: string; emptyDescription: string; data: UsageBreakdownItem[]; initialLoading: boolean; refreshing: boolean; error: string; onRetry: () => void }) {
  return <section className="usage-models card">
    <div className="card-heading"><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2></div>{refreshing && <UsageRefreshBadge />}</div>
    {error && !data.length ? <ErrorState message={error} onRetry={onRetry} /> : initialLoading ? <LoadingState /> : data.length ? <div className="usage-table-wrap"><table><thead><tr><th>名称</th><th>响应数</th><th>Tokens</th><th>命中率</th><th>总成本</th><th>平均成本</th></tr></thead><tbody>{data.map((item) => <tr key={item.id}><th><span title={item.path || item.title}>{item.title}</span>{item.path && <small title={item.path}>{item.path}</small>}</th><td>{item.requests.toLocaleString()}</td><td>{item.tokens.toLocaleString()}</td><td>{usageRate(item.cache_hit_rate).toFixed(1)}%</td><td>{formatCost(item.total_cost_usd)}</td><td>{formatCost(item.avg_cost_usd)}</td></tr>)}</tbody></table></div> : <EmptyState icon={ChartNoAxesCombined} title="暂无用量记录" description={emptyDescription} />}
  </section>
}


export { UsagePage }
