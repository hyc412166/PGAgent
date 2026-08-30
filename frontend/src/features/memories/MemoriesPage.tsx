import { AlertCircle, CheckCircle2, Database, History, Search, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { api, describeError } from '../../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, ToggleSwitch } from '../../components/ui'
import { useApiData } from '../../shared/hooks/useApiData'
import { formatDate } from '../../shared/lib/display'
import type { MemoryRecord, MemorySettings } from '../../types'
type MemoryRecall = { message_id: string; session_id: string; turn_id?: string; request: string; memories: Array<{ id: string; name?: string; memory_type?: string }>; created_at?: string }

function MemoriesPage() {
  const memories = useApiData<MemoryRecord[]>([], () => api.list<MemoryRecord>('/api/memories?memory_status=active', ['memories']), [])
  const recalls = useApiData<MemoryRecall[]>([], () => api.list<MemoryRecall>('/api/memory-recalls?limit=20', ['memory_recalls']), [])
  const memorySettings = useApiData<MemorySettings | null>(null, () => api.get<MemorySettings>('/api/memories/settings'), [])
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [memorySaving, setMemorySaving] = useState(false)
  const [memorySaveError, setMemorySaveError] = useState('')
  const [memorySaved, setMemorySaved] = useState(false)
  const visible = memories.data.filter((item) => {
    const needle = query.trim().toLocaleLowerCase()
    return !needle || `${item.name} ${item.description} ${item.content} ${item.tags.join(' ')}`.toLocaleLowerCase().includes(needle)
  })

  async function saveMemorySettings(enabled: boolean) {
    if (memorySaving) return
    setMemorySaving(true); setMemorySaveError(''); setMemorySaved(false)
    try {
      const result = await api.put<MemorySettings>('/api/memories/settings', { enabled })
      memorySettings.setState({ data: result, loading: false, error: '' })
      setMemorySaved(true)
    } catch (cause) {
      setMemorySaveError(describeError(cause))
    } finally {
      setMemorySaving(false)
    }
  }

  async function archiveMemory(id: string) {
    setError('')
    try { await api.delete(`/api/memories/${encodeURIComponent(id)}`); await memories.reload() }
    catch (cause) { setError(describeError(cause)) }
  }

  return <div className="page memory-settings-page">
    <PageHeader eyebrow="CONVERSATION MEMORY" title="持久记忆" description="系统自动提取并召回可复用信息；每个新问题只召回相关记录，并把当轮结果冻结在用户消息中。" />
    <section className="personalization-memory-card card">
      <div className="card-heading"><div><p className="eyebrow">MEMORY CONTROL</p><h2>持久记忆开关</h2></div></div>
      {memorySettings.initialLoading ? <LoadingState label="正在读取持久记忆设置" /> : memorySettings.error ? <ErrorState message={memorySettings.error} onRetry={memorySettings.reload} /> : memorySettings.data && <>
        <div className="memory-global-setting">
          <div className="memory-global-copy"><strong>全局记忆</strong><p>关闭后，所有会话都不会读取已有记忆，也不会生成新的记忆。</p></div>
          <div className="memory-global-control">
            <span className={`memory-global-status ${memorySettings.data.enabled ? 'is-on' : ''}`} role="status">{memorySettings.data.enabled ? '已启用' : '已关闭'}</span>
            <ToggleSwitch checked={memorySettings.data.enabled} label="全局记忆" busy={memorySaving} onChange={(enabled) => void saveMemorySettings(enabled)} />
          </div>
        </div>
        {(memorySaveError || memorySaved) && <div className="memory-global-feedback">
          {memorySaveError && <span className="inline-error" role="alert"><AlertCircle size={15} />{memorySaveError}</span>}
          {memorySaved && <span className="personalization-saved"><CheckCircle2 size={15} />持久记忆设置已保存</span>}
        </div>}
      </>}
    </section>
    <section className="memory-list-card card">
      <div className="card-heading"><div><p className="eyebrow">ACTIVE INDEX</p><h2>有效记忆</h2></div><label className="memory-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、标签或内容" /></label></div>
      {error && <div className="inline-error"><AlertCircle size={15} />{error}</div>}
      {memories.error && !memories.data.length ? <ErrorState message={memories.error} onRetry={memories.reload} /> : memories.initialLoading ? <LoadingState /> : visible.length ? <div className="memory-grid">{visible.map((item) => <article className="memory-record" key={item.id}>
        <header><div><strong>{item.name}</strong><span>{item.scope} · {item.memory_type}{item.pinned ? ' · pinned' : ''}</span></div><button className="icon-button" type="button" title="归档" aria-label={`归档 ${item.name}`} onClick={() => void archiveMemory(item.id)}><Trash2 size={15} /></button></header>
        {item.description && <p className="memory-description">{item.description}</p>}
        <pre>{item.content}</pre>
        {!!item.tags.length && <footer>{item.tags.map((tag) => <span key={tag}>{tag}</span>)}</footer>}
        {(item.source_session_id || item.source_turn_id) && <small>来源：{item.source_session_id ? `会话 ${item.source_session_id}` : ''}{item.source_turn_id ? ` / 轮次 ${item.source_turn_id}` : ''}</small>}
      </article>)}</div> : <EmptyState icon={Database} title="暂无匹配记忆" description="完成对话后，系统会自动提取可复用信息。" />}
    </section>
    <section className="memory-list-card card">
      <div className="card-heading"><div><p className="eyebrow">TURN SNAPSHOTS</p><h2>最近逐轮召回</h2></div></div>
      {recalls.initialLoading ? <LoadingState /> : recalls.data.length ? <div className="memory-recall-list">{recalls.data.map((recall) => <article key={recall.message_id}>
        <div><strong>{recall.request}</strong><small>{formatDate(recall.created_at)} · {recall.session_id}</small></div>
        <p>{recall.memories.length ? recall.memories.map((item) => item.name || item.id).join('、') : '本轮没有召回相关记忆'}</p>
      </article>)}</div> : <EmptyState icon={History} title="暂无逐轮快照" description="新问题首次送模后，这里会展示该轮实际冻结的召回结果。" />}
    </section>
  </div>
}


export { MemoriesPage }
