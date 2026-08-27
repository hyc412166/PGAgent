import { AlertCircle, Database, History, LoaderCircle, Plus, Search, Trash2 } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { api, describeError } from '../../api'
import { EmptyState, ErrorState, Field, LoadingState, PageHeader } from '../../components/ui'
import { useApiData } from '../../shared/hooks/useApiData'
import { formatDate } from '../../shared/lib/display'
import type { MemoryRecord, Workspace } from '../../types'
type MemoryRecall = { message_id: string; session_id: string; turn_id?: string; request: string; memories: Array<{ id: string; name?: string; memory_type?: string }>; created_at?: string }

function MemoriesPage() {
  const memories = useApiData<MemoryRecord[]>([], () => api.list<MemoryRecord>('/api/memories?memory_status=active', ['memories']), [])
  const recalls = useApiData<MemoryRecall[]>([], () => api.list<MemoryRecall>('/api/memory-recalls?limit=20', ['memory_recalls']), [])
  const workspaces = useApiData<Workspace[]>([], () => api.list<Workspace>('/api/workspaces', ['workspaces']), [])
  const [query, setQuery] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const visible = memories.data.filter((item) => {
    const needle = query.trim().toLocaleLowerCase()
    return !needle || `${item.name} ${item.description} ${item.content} ${item.tags.join(' ')}`.toLocaleLowerCase().includes(needle)
  })

  async function createMemory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const scope = String(form.get('scope') || 'global')
    const scopeId = String(form.get('scope_id') || '') || null
    setSaving(true); setError('')
    try {
      await api.post('/api/memories', {
        scope,
        scope_id: scope === 'global' ? null : scopeId,
        name: String(form.get('name') || '').trim(),
        memory_type: String(form.get('memory_type') || 'project'),
        description: String(form.get('description') || '').trim(),
        content: String(form.get('content') || '').trim(),
        tags: String(form.get('tags') || '').split(',').map((tag) => tag.trim()).filter(Boolean),
        pinned: form.get('pinned') === 'on',
      })
      event.currentTarget.reset()
      await memories.reload()
    } catch (cause) { setError(describeError(cause)) } finally { setSaving(false) }
  }

  async function archiveMemory(id: string) {
    setError('')
    try { await api.delete(`/api/memories/${encodeURIComponent(id)}`); await memories.reload() }
    catch (cause) { setError(describeError(cause)) }
  }

  return <div className="page memory-settings-page">
    <PageHeader eyebrow="CONVERSATION MEMORY" title="持久记忆" description="SQLite 是唯一真源；每个新问题只召回相关记录，并把当轮结果冻结在用户消息中。" />
    <section className="memory-create-card card">
      <div className="card-heading"><div><p className="eyebrow">NEW MEMORY</p><h2>新增或更新记忆</h2></div></div>
      <form className="memory-form" onSubmit={createMemory}>
        <Field label="名称"><input name="name" required maxLength={200} /></Field>
        <Field label="类型"><select name="memory_type" defaultValue="project"><option value="user">用户偏好</option><option value="feedback">反馈</option><option value="project">项目</option><option value="reference">参考资料</option></select></Field>
        <Field label="作用域"><select name="scope" defaultValue="global"><option value="global">全局</option><option value="workspace">项目</option></select></Field>
        <Field label="项目"><select name="scope_id" defaultValue=""><option value="">仅全局记忆无需选择</option>{workspaces.data.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
        <Field label="说明"><input name="description" maxLength={1000} /></Field>
        <Field label="标签（逗号分隔）"><input name="tags" /></Field>
        <Field label="内容"><textarea name="content" required rows={5} /></Field>
        <label className="memory-pin"><input type="checkbox" name="pinned" /> 固定优先级</label>
        <div className="memory-form-actions"><button className="primary-button" type="submit" disabled={saving}>{saving ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />}保存</button></div>
      </form>
      {error && <div className="inline-error"><AlertCircle size={15} />{error}</div>}
    </section>
    <section className="memory-list-card card">
      <div className="card-heading"><div><p className="eyebrow">ACTIVE INDEX</p><h2>有效记忆</h2></div><label className="memory-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、标签或内容" /></label></div>
      {memories.error && !memories.data.length ? <ErrorState message={memories.error} onRetry={memories.reload} /> : memories.initialLoading ? <LoadingState /> : visible.length ? <div className="memory-grid">{visible.map((item) => <article className="memory-record" key={item.id}>
        <header><div><strong>{item.name}</strong><span>{item.scope} · {item.memory_type}{item.pinned ? ' · pinned' : ''}</span></div><button className="icon-button" type="button" title="归档" aria-label={`归档 ${item.name}`} onClick={() => void archiveMemory(item.id)}><Trash2 size={15} /></button></header>
        {item.description && <p className="memory-description">{item.description}</p>}
        <pre>{item.content}</pre>
        {!!item.tags.length && <footer>{item.tags.map((tag) => <span key={tag}>{tag}</span>)}</footer>}
        {(item.source_session_id || item.source_turn_id) && <small>来源：{item.source_session_id ? `会话 ${item.source_session_id}` : ''}{item.source_turn_id ? ` / 轮次 ${item.source_turn_id}` : ''}</small>}
      </article>)}</div> : <EmptyState icon={Database} title="暂无匹配记忆" description="显式保存，或在完成对话后由后台提取可复用信息。" />}
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
