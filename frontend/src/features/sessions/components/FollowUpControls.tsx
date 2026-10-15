import { Check, CornerDownRight, ListPlus, MoreHorizontal, Square } from 'lucide-react'
import { useRef } from 'react'

// 默认发送方式放入次级菜单；有草稿时，停止仍可独立访问且不会提交草稿。
export function FollowUpControls({ mode, disabled, onModeChange, onStop }: {
  mode: 'follow_up' | 'steer'
  disabled: boolean
  onModeChange: (mode: 'follow_up' | 'steer') => void
  onStop?: () => void
}) {
  const menu = useRef<HTMLDetailsElement>(null)
  function close() {
    if (menu.current) menu.current.open = false
    menu.current?.querySelector('summary')?.focus()
  }
  return <details ref={menu} className="follow-up-options" onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false
  }} onKeyDown={(event) => {
    if (event.key === 'Escape') { event.preventDefault(); close() }
  }}>
    <summary aria-label="运行中发送选项" title={`运行中发送：${mode === 'follow_up' ? '排队' : 'Steer'}`}><MoreHorizontal size={16} /></summary>
    <div className="follow-up-menu" aria-label="运行中发送选项">
      <span className="follow-up-menu-label">运行中发送</span>
      <button type="button" aria-pressed={mode === 'follow_up'} disabled={disabled} onClick={() => { onModeChange('follow_up'); close() }}><ListPlus size={15} /><span>排队发送</span>{mode === 'follow_up' && <Check size={14} />}</button>
      <button type="button" aria-pressed={mode === 'steer'} disabled={disabled} onClick={() => { onModeChange('steer'); close() }}><CornerDownRight size={15} /><span>Steer · 补充当前任务</span>{mode === 'steer' && <Check size={14} />}</button>
      {onStop && <button type="button" className="follow-up-menu-stop" onClick={() => { onStop(); close() }}><Square size={13} /><span>停止当前任务</span></button>}
    </div>
  </details>
}
