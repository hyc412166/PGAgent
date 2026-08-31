import { AlertCircle, KeyRound, LoaderCircle, Pencil, Plus, RefreshCw, Sparkles, Trash2 } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { api, describeError } from '../../api'
import { resolveEffectiveThinking } from '../../composerSettings'
import { availableConnectionModels } from '../../modelSettings'
import { EmptyState, ErrorState, Field, LoadingState, PageHeader, SlidePanel, StatusBadge, ToggleSwitch } from '../../components/ui'
import { useApiData } from '../../shared/hooks/useApiData'
import type { Connection } from '../../types'

function ModelsPage() {
  const connections = useApiData<Connection[]>([], () => api.list<Connection>('/api/connections', ['connections']), [])
  const [panelOpen, setPanelOpen] = useState(false)
  const [editingConnection, setEditingConnection] = useState<Connection | null>(null)
  const [saving, setSaving] = useState(false)
  const [discovering, setDiscovering] = useState('')
  const [togglingConnectionId, setTogglingConnectionId] = useState('')
  const [updatingThinkingId, setUpdatingThinkingId] = useState('')
  const [deletingConnectionId, setDeletingConnectionId] = useState('')
  const [formError, setFormError] = useState('')
  const [discoveryError, setDiscoveryError] = useState<Record<string, string>>({})
  const connectionMutationBusy = saving || Boolean(discovering || togglingConnectionId || updatingThinkingId || deletingConnectionId)

  function openConnectionPanel(connection: Connection | null = null) {
    if (connectionMutationBusy) return
    setEditingConnection(connection)
    setFormError('')
    setPanelOpen(true)
  }

  function closeConnectionPanel() {
    if (saving) return
    setPanelOpen(false)
    setEditingConnection(null)
    setFormError('')
  }

  async function saveConnection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (connectionMutationBusy) return
    const form = new FormData(event.currentTarget); setSaving(true); setFormError('')
    try {
      const manualModel = String(form.get('default_model') || '').trim()
      const apiKey = String(form.get('api_key') || '').trim()
      const payload: Record<string, unknown> = { name: form.get('name'), base_url: form.get('base_url'), provider: form.get('provider'), default_model: manualModel || null, manual_models: manualModel ? [manualModel] : [], thinking_level: form.get('thinking_level') }
      if (apiKey) payload.api_key = apiKey
      if (editingConnection) {
        if (payload.base_url === editingConnection.base_url) delete payload.base_url
        await api.patch(`/api/connections/${editingConnection.id}`, payload)
      } else {
        await api.post('/api/connections', payload)
      }
      setPanelOpen(false); setEditingConnection(null); await connections.reload()
    } catch (error) { setFormError(describeError(error)) } finally { setSaving(false) }
  }

  async function deleteConnection(connection: Connection) {
    if (connectionMutationBusy || !window.confirm(`确定删除模型连接“${connection.name}”吗？使用它的 Agent 和会话会改为自动选择其他可用连接，此操作不可撤销。`)) return
    setDeletingConnectionId(connection.id)
    setDiscoveryError((value) => ({ ...value, [connection.id]: '' }))
    try {
      await api.delete(`/api/connections/${connection.id}`)
      if (editingConnection?.id === connection.id) closeConnectionPanel()
      await connections.reload()
    } catch (error) {
      setDiscoveryError((value) => ({ ...value, [connection.id]: describeError(error) }))
    } finally { setDeletingConnectionId('') }
  }

  async function discover(connection: Connection) {
    if (connectionMutationBusy) return
    setDiscovering(connection.id); setDiscoveryError((value) => ({ ...value, [connection.id]: '' }))
    try { await api.post(`/api/connections/${connection.id}/test`); await connections.reload() }
    catch (error) { setDiscoveryError((value) => ({ ...value, [connection.id]: describeError(error) })) } finally { setDiscovering('') }
  }

  async function updateThinking(connection: Connection, level: string) {
    if (connectionMutationBusy) return
    setUpdatingThinkingId(connection.id)
    setDiscoveryError((value) => ({ ...value, [connection.id]: '' }))
    try {
      const saved = await api.patch<Connection>(`/api/connections/${connection.id}`, { thinking_level: level })
      connections.setState({ data: connections.data.map((item) => item.id === saved.id ? saved : item), loading: false, error: '' })
    } catch (error) {
      setDiscoveryError((value) => ({ ...value, [connection.id]: describeError(error) }))
    } finally { setUpdatingThinkingId('') }
  }

  async function toggleConnection(connection: Connection, enabled: boolean) {
    if (connectionMutationBusy) return
    setTogglingConnectionId(connection.id)
    setDiscoveryError((value) => ({ ...value, [connection.id]: '' }))
    try {
      const saved = await api.patch<Connection>(`/api/connections/${connection.id}`, { enabled })
      connections.setState({ data: connections.data.map((item) => item.id === saved.id ? saved : item), loading: false, error: '' })
    } catch (error) {
      setDiscoveryError((value) => ({ ...value, [connection.id]: describeError(error) }))
    } finally {
      setTogglingConnectionId('')
    }
  }

  return (
    <div className="page">
      <PageHeader eyebrow="模型舱" title="模型设置" description="配置 Base URL 与 API Key，控制每条连接是否参与运行，并统一思考强度。" action={<button className="button button-primary" disabled={connectionMutationBusy} onClick={() => openConnectionPanel()}><Plus size={16} />添加连接</button>} />
      <div className="security-note"><KeyRound size={19} /><div><strong>密钥不会明文保存到 SQLite</strong><p>API Key 进入 Windows 凭据存储，数据库仅保留引用和末尾提示。</p></div></div>
      {connections.error ? <ErrorState message={connections.error} onRetry={connections.reload} /> : connections.loading ? <LoadingState /> : connections.data.length ? (
        <section className="connection-list">
          {connections.data.map((connection) => (
            <article className="connection-card" key={connection.id}>
              <header><div className="provider-icon"><Sparkles size={20} /></div><div><h2>{connection.name}</h2><p>{connection.provider || 'OpenAI Compatible'}</p></div><div className="connection-card-actions"><StatusBadge status={connection.status || 'unknown'} /><ToggleSwitch checked={connection.enabled !== false} label={`模型连接 ${connection.name}`} busy={togglingConnectionId === connection.id} disabled={connectionMutationBusy} onChange={(enabled) => void toggleConnection(connection, enabled)} /><button type="button" className="icon-button" aria-label={`编辑 ${connection.name}`} title="修改模型 API" disabled={connectionMutationBusy} onClick={() => openConnectionPanel(connection)}><Pencil size={15} /></button><button type="button" className="icon-button danger-icon" aria-label={`删除 ${connection.name}`} title="删除模型 API" disabled={connectionMutationBusy} onClick={() => void deleteConnection(connection)}>{deletingConnectionId === connection.id ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />}</button></div></header>
              <dl><div><dt>Base URL</dt><dd>{connection.base_url || '—'}</dd></div><div><dt>API Key</dt><dd>{connection.api_key_hint || '已安全保存'}</dd></div><div><dt>默认模型</dt><dd>{connection.default_model || '尚未选择'}</dd></div></dl>
              <div className="model-block"><div className="model-block-head"><strong>可用模型</strong><button className="button button-quiet" onClick={() => discover(connection)} disabled={connectionMutationBusy}>{discovering === connection.id ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}发现模型</button></div>{availableConnectionModels(connection).length ? <div className="model-tags">{availableConnectionModels(connection).map((model) => <span className={model === connection.default_model ? 'selected' : ''} key={`${connection.id}:${model}`}>{model}</span>)}</div> : <p className="muted">尚未获取模型列表，可点击发现或使用手动模型 ID。</p>}</div>
              <div className="thinking-row"><div><strong>思考强度</strong><small>不同提供商会映射到各自支持的参数</small></div><select aria-label={`${connection.name} 思考强度`} value={resolveEffectiveThinking(connection.thinking_level)} disabled={connectionMutationBusy} onChange={(event) => void updateThinking(connection, event.target.value)}><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高</option></select></div>
              {(discoveryError[connection.id] || connection.last_error) && <p className="inline-error"><AlertCircle size={14} />{discoveryError[connection.id] || connection.last_error}</p>}
            </article>
          ))}
        </section>
      ) : <EmptyState icon={Sparkles} title="添加模型连接" description="支持 OpenRouter、DeepSeek 和大多数 OpenAI 兼容中转服务。" action={<button className="button button-primary" onClick={() => openConnectionPanel()}><Plus size={16} />添加连接</button>} />}
      {panelOpen && <SlidePanel title={editingConnection ? '修改模型 API' : '添加模型连接'} description={editingConnection ? '修改连接信息；API Key 留空会继续使用已安全保存的密钥。' : '保存后会尝试请求 /models；失败时仍可使用手动模型 ID。'} onClose={closeConnectionPanel}><form key={editingConnection?.id || 'new-connection'} className="panel-form" onSubmit={saveConnection} autoComplete="off"><div className="form-row"><Field label="连接名称"><input name="name" required placeholder="例如：DeepSeek" defaultValue={editingConnection?.name || ''} autoFocus /></Field><Field label="提供商"><select name="provider" defaultValue={editingConnection?.provider || 'openai_compatible'}><option value="openai_compatible">OpenAI 兼容</option><option value="openrouter">OpenRouter</option><option value="deepseek">DeepSeek</option></select></Field></div><Field label="Base URL"><input name="base_url" type="url" required placeholder="https://api.example.com/v1" defaultValue={editingConnection?.base_url || ''} /></Field><Field label="API Key" hint={editingConnection ? '留空则保持现有密钥；填写新值会覆盖系统凭据' : '只发送给你填写的 Base URL，并写入系统凭据存储'}><input name="api_key" type="password" required={!editingConnection} placeholder={editingConnection ? '留空以保持现有密钥' : 'sk-…'} autoComplete="new-password" /></Field><div className="form-row"><Field label="默认 / 手动模型 ID" hint="当 /models 不可用时也会使用这个模型"><input name="default_model" placeholder="例如：deepseek-chat" defaultValue={editingConnection?.default_model || ''} /></Field><Field label="思考强度"><select name="thinking_level" defaultValue={resolveEffectiveThinking(editingConnection?.thinking_level)}><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高</option></select></Field></div>{formError && <p className="form-error" role="alert">{formError}</p>}<div className="form-actions"><button type="button" className="button button-secondary" onClick={closeConnectionPanel}>取消</button><button className="button button-primary" disabled={connectionMutationBusy}>{saving && <LoaderCircle className="spin" size={15} />}{editingConnection ? '保存修改' : '保存并检测'}</button></div></form></SlidePanel>}
    </div>
  )
}


export { ModelsPage }
