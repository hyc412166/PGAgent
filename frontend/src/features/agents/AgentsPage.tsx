import { Bot, ChevronRight, LoaderCircle, Pencil, Plus, Trash2, Wrench } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { api, describeError } from '../../api'
import { toggleSelectedId } from '../../capabilitySelection'
import { CapabilityMultiSelect, EmptyState, ErrorState, Field, LoadingState, PageHeader, SlidePanel, StatusBadge } from '../../components/ui'
import { agentTemplates } from './templates'
import type { AgentTemplate } from './templates'
import { useApiData } from '../../shared/hooks/useApiData'
import { formatDate } from '../../shared/lib/display'
import type { AgentProfile, SkillCatalogItem, ToolCatalogItem } from '../../types'

function AgentsPage() {
  const agents = useApiData<AgentProfile[]>([], () => api.list<AgentProfile>('/api/agents', ['agents']), [])
  const tools = useApiData<ToolCatalogItem[]>([], () => api.list<ToolCatalogItem>('/api/tools', ['tools']), [])
  const skills = useApiData<SkillCatalogItem[]>([], () => api.list<SkillCatalogItem>('/api/skills', ['skills']), [])
  const [panelOpen, setPanelOpen] = useState(false)
  const [editing, setEditing] = useState<AgentProfile | null>(null)
  const [selectedToolIds, setSelectedToolIds] = useState<string[]>([])
  const [selectedSkillIds, setSelectedSkillIds] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [deletingId, setDeletingId] = useState('')
  const [formError, setFormError] = useState('')
  const [template, setTemplate] = useState<AgentTemplate | null>(null)
  const childAgents = agents.data.filter((agent) => !agent.is_default)

  function closeAgentPanel() {
    setPanelOpen(false)
  }

  function openAgentPanel(agent?: AgentProfile) {
    if (agent?.is_default) return
    setEditing(agent ?? null)
    setTemplate(null)
    setSelectedToolIds(agent?.tool_ids ?? [])
    setSelectedSkillIds(agent?.skill_ids ?? [])
    setFormError('')
    setPanelOpen(true)
  }

  function openTemplatePanel(nextTemplate: AgentTemplate) {
    setEditing(null)
    setTemplate(nextTemplate)
    setSelectedToolIds(nextTemplate.toolIds.filter((id) => tools.data.some((item) => item.id === id)))
    setSelectedSkillIds([])
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
        model_connection_id: null,
        model_id: null,
        thinking_level: 'auto',
        tool_ids: selectedToolIds,
        skill_ids: selectedSkillIds,
      }
      if (editing) await api.patch(`/api/agents/${editing.id}`, payload)
      else await api.post('/api/agents', payload)
      setPanelOpen(false); await agents.reload()
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
      <PageHeader eyebrow="企鹅小队" title="小队成员" description="给每只企鹅分配清晰的角色和能力，让协作轻巧又可靠。" action={<button className="button button-primary" onClick={() => openAgentPanel()}><Plus size={16} />创建子 Agent</button>} />
      <section className="agent-starter-strip" aria-label="专业 Agent 模板">
        <div className="agent-starter-copy"><span className="eyebrow">快速组队</span><strong>挑一只合适的企鹅</strong><p>模板只会预填角色和能力，保存前仍可逐项调整。</p></div>
        <div className="agent-template-list">
          {agentTemplates.map((item) => <button type="button" key={item.id} className={`agent-template-card template-${item.accent}`} onClick={() => openTemplatePanel(item)}>
            <span className="agent-template-icon"><Bot size={16} /></span><span><strong>{item.name}</strong><small>{item.description}</small></span><ChevronRight size={15} />
          </button>)}
        </div>
      </section>
      {formError && !panelOpen && <p className="form-error page-form-error" role="alert">{formError}</p>}
      {agents.error ? <ErrorState message={agents.error} onRetry={agents.reload} /> : agents.loading ? <LoadingState /> : childAgents.length ? (
        <section className="entity-grid agent-grid">
          {childAgents.map((agent) => (
            <article className="entity-card agent-card" key={agent.id}>
              <div className="agent-head"><div className="agent-avatar"><Bot size={22} /></div><div className="agent-card-actions"><StatusBadge status={agent.status || 'idle'} /><button className="icon-button" aria-label={`编辑 ${agent.name}`} title="编辑子 Agent" onClick={() => openAgentPanel(agent)}><Pencil size={15} /></button><button className="icon-button danger-icon" aria-label={`删除 ${agent.name}`} disabled={deletingId === agent.id} title="删除子 Agent" onClick={() => void deleteAgent(agent)}>{deletingId === agent.id ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />}</button></div></div>
              <h2>{agent.name}</h2><p className="agent-role">{agent.role || '通用执行子 Agent'}</p><p>{agent.description || '暂无角色说明'}</p>
              <div className="agent-meta"><span><Wrench size={14} />{agent.tool_ids?.length ?? 0} 工具 · {agent.skill_ids?.length ?? 0} Skill</span></div>
              <footer><span>{formatDate(agent.created_at)}</span></footer>
            </article>
          ))}
        </section>
      ) : <EmptyState icon={Bot} title="创建你的第一个子 Agent" description="定义专业角色、系统指令与可用能力；主 Agent 会在复杂、专业或你明确要求时委派匹配的子 Agent。" action={<button className="button button-primary" onClick={() => openAgentPanel()}><Plus size={16} />创建子 Agent</button>} />}
      <SlidePanel open={panelOpen} title={editing ? '编辑子 Agent' : template ? `使用「${template.name}」模板` : '创建子 Agent'} description="配置角色以及允许这个子 Agent 使用的工具和 Skill。模型与推理强度由主 Agent 统一管理。" onClose={closeAgentPanel} onExited={() => { setEditing(null); setTemplate(null) }}>
        <form className="panel-form" onSubmit={saveAgent} key={editing?.id || 'new-agent'}>
          <Field label="名称"><input name="name" required placeholder="例如：代码协作者" autoFocus defaultValue={editing?.name || template?.name || ''} /></Field>
          <Field label="简介"><input name="description" placeholder="简要描述擅长处理的任务" defaultValue={editing?.description || template?.description || ''} /></Field>
          <Field label="系统指令"><textarea name="system_prompt" rows={6} placeholder="说明工作原则、输出风格和边界……" defaultValue={editing?.system_prompt || template?.systemPrompt || ''} /></Field>
          <CapabilityMultiSelect label="工具" items={tools.data.map((item) => ({ ...item, name: item.label || item.name }))} selectedIds={selectedToolIds} loading={tools.loading} error={tools.error} onRetry={() => void tools.reload()} onToggle={(id) => setSelectedToolIds((current) => toggleSelectedId(current, id))} />
          <CapabilityMultiSelect label="Skill" items={skills.data} selectedIds={selectedSkillIds} loading={skills.loading} error={skills.error} onRetry={() => void skills.reload()} onToggle={(id) => setSelectedSkillIds((current) => toggleSelectedId(current, id))} />
          {formError && <p className="form-error" role="alert">{formError}</p>}
          <div className="form-actions"><button type="button" className="button button-secondary" onClick={closeAgentPanel}>取消</button><button className="button button-primary" disabled={saving}>{saving && <LoaderCircle className="spin" size={15} />}{editing ? '保存修改' : '创建'}</button></div>
        </form>
      </SlidePanel>
    </div>
  )
}


export { AgentsPage }
