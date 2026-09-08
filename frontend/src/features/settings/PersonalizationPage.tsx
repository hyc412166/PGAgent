// 本文件实现 PersonalizationPage 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
import { AlertCircle, Check, CheckCircle2, LoaderCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api, describeError } from '../../api'
import { ErrorState, LoadingState, PageHeader } from '../../components/ui'
import { useApiData } from '../../shared/hooks/useApiData'
import type { PersonalizationSettings } from '../../types'

// PersonalizationPage 读取并保存用户自定义指令，后端会在后续 Agent 上下文准备时使用它。
function PersonalizationPage() {
  const personalization = useApiData<PersonalizationSettings | null>(null, () => api.get<PersonalizationSettings>('/api/system/personalization'), [])
  // draft 与已保存值分离；saving/error/saved 提供一次提交的完整可见反馈。
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (personalization.data) setDraft(personalization.data.custom_instructions)
  }, [personalization.data])

  async function savePersonalization() {
    setSaving(true); setSaveError(''); setSaved(false)
    try {
      const result = await api.put<PersonalizationSettings>('/api/system/personalization', { custom_instructions: draft })
      personalization.setState({ data: result, loading: false, error: '' })
      setDraft(result.custom_instructions)
      setSaved(true)
    } catch (cause) {
      setSaveError(describeError(cause))
    } finally {
      setSaving(false)
    }
  }

  const maximum = personalization.data?.max_characters ?? 32768
  const changed = personalization.data !== null && draft !== personalization.data.custom_instructions
  return <div className="page personalization-settings-page">
    <PageHeader eyebrow="GLOBAL AGENTS.MD" title="个性化" description="为所有新任务设置固定工作原则、回答偏好和长期约束。内容保存在全局 AGENTS.md，而不是持久记忆。" />
    <section className="personalization-card card">
      <div className="card-heading"><div><p className="eyebrow">CUSTOM INSTRUCTIONS</p><h2>个人指令</h2></div><span className="personalization-count">{draft.length.toLocaleString()} / {maximum.toLocaleString()}</span></div>
      {personalization.initialLoading ? <LoadingState /> : personalization.error ? <ErrorState message={personalization.error} onRetry={personalization.reload} /> : <>
        <p className="personalization-help">每个新 Run 会在模型调用前读取并冻结这些规则。项目根目录中的 AGENTS.md 会排在个人指令之后，因此可以提供更具体的项目约束；已经运行中的任务不会被即时改写。</p>
        {personalization.data?.override_active && <div className="personalization-warning"><AlertCircle size={16} /><div><strong>AGENTS.override.md 正在生效</strong><span>当前有效内容来自 {personalization.data.effective_path}。这里保存的 AGENTS.md 会在移除 override 后恢复生效。</span></div></div>}
        <textarea
          className="personalization-editor"
          aria-label="全局个人指令"
          value={draft}
          maxLength={maximum}
          rows={16}
          placeholder={'例如：\n- 默认使用中文解释\n- 修改代码后运行相关测试\n- 涉及架构变化时先说明取舍'}
          onChange={(event) => { setDraft(event.target.value); setSaved(false) }}
        />
        <div className="personalization-meta"><span>保存位置</span><code>{personalization.data?.agents_path}</code></div>
        <div className="personalization-actions">
          {saveError && <div className="inline-error"><AlertCircle size={15} />{saveError}</div>}
          {saved && <span className="personalization-saved"><CheckCircle2 size={15} />已保存，将从下一次新运行开始生效</span>}
          <button className="button button-secondary" type="button" disabled={!changed || saving} onClick={() => { setDraft(personalization.data?.custom_instructions ?? ''); setSaved(false) }}>撤销修改</button>
          <button className="button button-primary" type="button" disabled={!changed || saving || draft.length > maximum} onClick={() => void savePersonalization()}>{saving ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}保存指令</button>
        </div>
      </>}
    </section>
  </div>
}

export { PersonalizationPage }
