import { AlertCircle, Cable, CirclePlus, Command, LoaderCircle, Pencil, Plus, Search, ServerCog, Trash2, X } from 'lucide-react'
import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { api, describeError } from '../../api'
import { EmptyState, ErrorState, Field, LoadingState, PageHeader, SlidePanel, ToggleSwitch } from '../../components/ui'
import { filterMcpServers, mcpStatusLabel } from '../../mcpPresentation'
import { useApiData } from '../../shared/hooks/useApiData'
import type { McpServer } from '../../types'

type ServerPayload = Pick<McpServer, 'name' | 'command' | 'args' | 'enabled'>

function McpPage() {
  const servers = useApiData<McpServer[]>([], () => api.list<McpServer>('/api/mcp/servers'), [])
  const [query, setQuery] = useState('')
  const [panelOpen, setPanelOpen] = useState(false)
  const [editing, setEditing] = useState<McpServer | null>(null)
  const [args, setArgs] = useState<string[]>([])
  const [enabled, setEnabled] = useState(true)
  const [saving, setSaving] = useState(false)
  const [busyNames, setBusyNames] = useState<Set<string>>(() => new Set())
  const [confirmingDelete, setConfirmingDelete] = useState('')
  const [formError, setFormError] = useState('')
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const deleteConfirmButtonRef = useRef<HTMLButtonElement>(null)
  const deleteTriggerRefs = useRef<Record<string, HTMLButtonElement | null>>({})

  const filteredServers = useMemo(() => filterMcpServers(servers.data, query), [query, servers.data])
  const mutationsBusy = busyNames.size > 0

  useEffect(() => {
    if (confirmingDelete) deleteConfirmButtonRef.current?.focus()
  }, [confirmingDelete])

  function updateServerList(server: McpServer, previousName?: string) {
    const previous = previousName ?? server.name
    const data = [...servers.data.filter((item) => item.name !== previous), server]
      .sort((left, right) => left.name.localeCompare(right.name))
    servers.setState({ data, loading: false, error: '' })
  }

  function markBusy(name: string, busy: boolean) {
    setBusyNames((current) => {
      const next = new Set(current)
      if (busy) next.add(name)
      else next.delete(name)
      return next
    })
  }

  function cancelDelete(name: string) {
    setConfirmingDelete('')
    window.requestAnimationFrame(() => deleteTriggerRefs.current[name]?.focus())
  }

  function openPanel(server: McpServer | null = null) {
    if (server?.transport !== undefined && server.transport !== 'stdio') return
    setEditing(server)
    setArgs(server?.args ?? [])
    setEnabled(server?.enabled ?? true)
    setFormError('')
    setPanelOpen(true)
  }

  function closePanel() {
    if (saving) return
    setPanelOpen(false)
    setEditing(null)
    setArgs([])
    setFormError('')
  }

  async function saveServer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const payload: ServerPayload = {
      name: String(form.get('name') || '').trim(),
      command: String(form.get('command') || '').trim(),
      args,
      enabled,
    }
    setSaving(true)
    setFormError('')
    try {
      const saved = editing
        ? await api.put<McpServer>(`/api/mcp/servers/${encodeURIComponent(editing.name)}`, payload)
        : await api.post<McpServer>('/api/mcp/servers', payload)
      updateServerList(saved, editing?.name)
      setPanelOpen(false)
      setEditing(null)
      setArgs([])
    } catch (error) {
      setFormError(describeError(error))
    } finally {
      setSaving(false)
    }
  }

  async function toggleServer(server: McpServer, nextEnabled: boolean) {
    markBusy(server.name, true)
    setRowErrors((current) => ({ ...current, [server.name]: '' }))
    try {
      const saved = await api.patch<McpServer>(`/api/mcp/servers/${encodeURIComponent(server.name)}`, { enabled: nextEnabled })
      updateServerList(saved)
    } catch (error) {
      setRowErrors((current) => ({ ...current, [server.name]: describeError(error) }))
    } finally {
      markBusy(server.name, false)
    }
  }

  async function deleteServer(server: McpServer) {
    markBusy(server.name, true)
    setRowErrors((current) => ({ ...current, [server.name]: '' }))
    try {
      await api.delete(`/api/mcp/servers/${encodeURIComponent(server.name)}`)
      setConfirmingDelete('')
      servers.setState({ data: servers.data.filter((item) => item.name !== server.name), loading: false, error: '' })
    } catch (error) {
      setRowErrors((current) => ({ ...current, [server.name]: describeError(error) }))
    } finally {
      markBusy(server.name, false)
    }
  }

  return (
    <div className="page mcp-page">
      <PageHeader
        eyebrow="工具接入舱"
        title="MCP"
        description="管理 MCP 服务器；Agent 优先读取缓存目录，只在真正调用工具时按需连接。"
        action={<button className="button button-primary" type="button" disabled={mutationsBusy || servers.loading || Boolean(servers.error)} onClick={() => openPanel()}><Plus size={16} />添加服务器</button>}
      />

      <section className="mcp-overview" aria-label="MCP 服务器概览">
        <div className="mcp-overview-mark" aria-hidden="true"><Cable size={21} /></div>
        <div><strong>{servers.data.length} 个服务器</strong><span>{servers.data.filter((server) => server.enabled).length} 个可供新运行按需调用</span></div>
        <label className="mcp-search"><Search size={15} aria-hidden="true" /><span className="sr-only">搜索 MCP 服务器</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、命令或参数" /></label>
      </section>

      {servers.error ? <ErrorState message={servers.error} onRetry={servers.reload} /> : servers.loading ? <LoadingState label="正在读取 MCP 配置" /> : filteredServers.length ? (
        <section className="mcp-server-list" aria-label="MCP 服务器">
          {filteredServers.map((server) => {
            const busy = busyNames.has(server.name)
            const deleting = confirmingDelete === server.name
            return <article className={`mcp-server-card ${server.enabled ? 'is-enabled' : ''}`} key={server.name}>
              <div className="mcp-connector" aria-hidden="true"><span /><i /></div>
              <div className="mcp-server-main">
                <header>
                  <div className="mcp-server-icon"><ServerCog size={19} /></div>
                  <div className="mcp-server-title"><div><h2>{server.name}</h2><span className={`mcp-runtime-status status-${server.runtime_status}`}>{mcpStatusLabel(server)}</span></div><p>{server.transport === 'stdio' ? 'STDIO 本地进程' : '流式 HTTP'}</p></div>
                  <div className="mcp-server-controls">
                    <ToggleSwitch checked={server.enabled} label={`${server.enabled ? '停用' : '启用'} ${server.name}`} busy={busy} disabled={mutationsBusy && !busy} onChange={(value) => void toggleServer(server, value)} />
                    <button className="icon-button" type="button" aria-label={`编辑 ${server.name}`} title={server.transport === 'stdio' ? '编辑服务器' : '流式 HTTP 配置需在高级配置文件中编辑'} disabled={mutationsBusy || server.transport !== 'stdio'} onClick={() => openPanel(server)}><Pencil size={15} /></button>
                    <button ref={(element) => { deleteTriggerRefs.current[server.name] = element }} className="icon-button danger-icon" type="button" aria-label={`删除 ${server.name}`} title="删除服务器" disabled={mutationsBusy} onClick={() => setConfirmingDelete(server.name)}><Trash2 size={15} /></button>
                  </div>
                </header>
                <div className="mcp-command-line"><Command size={14} aria-hidden="true" /><code>{server.command || '远程 MCP 服务器'}{server.args.length ? ` ${server.args.join(' ')}` : ''}</code></div>
                <footer><span>{server.args.length} 个参数</span><span>{server.tool_count ? `${server.tool_count} 个工具` : '首次冷连接后发现工具'}</span></footer>
                {deleting && <div className="mcp-delete-confirm" role="alertdialog" aria-label={`确认删除 ${server.name}`} aria-describedby={`mcp-delete-description-${encodeURIComponent(server.name)}`}><AlertCircle size={16} /><span id={`mcp-delete-description-${encodeURIComponent(server.name)}`}>删除后，新会话将无法使用这个服务器。</span><button type="button" className="button button-quiet" disabled={busy} onClick={() => cancelDelete(server.name)}>取消</button><button ref={deleteConfirmButtonRef} type="button" className="button button-danger" disabled={busy} onClick={() => void deleteServer(server)}>{busy && <LoaderCircle className="spin" size={14} />}确认删除</button></div>}
                {rowErrors[server.name] && <p className="inline-error" role="alert"><AlertCircle size={14} />{rowErrors[server.name]}</p>}
              </div>
            </article>
          })}
        </section>
      ) : query ? <EmptyState icon={Search} title="没有匹配的服务器" description="换一个名称、命令或参数再试试。" /> : <EmptyState icon={Cable} title="连接第一个 MCP 服务器" description="只需填写名称、启动命令和参数；首次运行发现工具，之后有缓存时按需连接。" action={<button className="button button-primary" type="button" onClick={() => openPanel()}><CirclePlus size={16} />添加服务器</button>} />}

      {panelOpen && <SlidePanel title={editing ? '编辑 MCP 服务器' : '连接至自定义 MCP'} description="当前支持 STDIO。本地命令会由 PGAgent 启动，并通过标准输入输出交换 MCP 消息。" onClose={closePanel}>
        <form key={editing?.name ?? 'new-mcp-server'} className="panel-form mcp-server-form" onSubmit={saveServer} autoComplete="off">
          <Field label="名称" hint="用于区分服务器，也会成为工具名称的一部分"><input name="name" required maxLength={100} defaultValue={editing?.name ?? ''} placeholder="例如：filesystem" autoFocus /></Field>
          <Field label="启动命令" hint="填写可执行程序，例如 npx、uvx 或 python"><input name="command" required defaultValue={editing?.command ?? ''} placeholder="例如：npx" spellCheck={false} /></Field>
          <div className="mcp-arguments-field">
            <div className="mcp-field-heading"><div><strong>参数</strong><small>每个参数单独一行，并按这里的顺序传给启动命令</small></div><button type="button" className="button button-quiet" onClick={() => setArgs((current) => [...current, ''])}><Plus size={14} />添加参数</button></div>
            {args.length ? <div className="mcp-argument-list">{args.map((argument, index) => <div className="mcp-argument-row" key={index}><span aria-hidden="true">{index + 1}</span><input aria-label={`参数 ${index + 1}`} value={argument} placeholder={index === 0 ? '例如：-y' : '输入参数'} spellCheck={false} onChange={(event) => setArgs((current) => current.map((item, itemIndex) => itemIndex === index ? event.target.value : item))} /><button type="button" className="icon-button" aria-label={`删除参数 ${index + 1}`} onClick={() => setArgs((current) => current.filter((_, itemIndex) => itemIndex !== index))}><X size={15} /></button></div>)}</div> : <button type="button" className="mcp-arguments-empty" onClick={() => setArgs([''])}><Plus size={15} />这个服务器还没有启动参数</button>}
          </div>
          <div className="mcp-form-enabled"><div><strong>启用服务器</strong><small>启用后，新运行会读取工具目录，并在实际调用时按需连接</small></div><ToggleSwitch checked={enabled} label="启用 MCP 服务器" onChange={setEnabled} /></div>
          {formError && <p className="form-error" role="alert">{formError}</p>}
          <div className="form-actions"><button type="button" className="button button-secondary" disabled={saving} onClick={closePanel}>取消</button><button type="submit" className="button button-primary" disabled={saving}>{saving && <LoaderCircle className="spin" size={15} />}{editing ? '保存修改' : '添加服务器'}</button></div>
        </form>
      </SlidePanel>}
    </div>
  )
}

export { McpPage }
