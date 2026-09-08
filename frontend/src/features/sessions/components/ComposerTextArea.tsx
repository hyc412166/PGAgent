// 本文件实现 ComposerTextArea 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'

// ComposerTextAreaHandle 允许父页面按需读取/恢复文本，避免每次输入都重渲染庞大会话页。
export type ComposerTextAreaHandle = { getValue: () => string; clear: () => void; setValue: (value: string) => void; focus: () => void }

// ComposerTextArea 在组件内部持有输入值，仅通过句柄和“是否有值”信号与 SessionsPage 通信。
export const ComposerTextArea = forwardRef<ComposerTextAreaHandle, { disabled: boolean; resetKey: string; placeholder: string; onHasValueChange: (hasValue: boolean) => void }>(function ComposerTextArea({ disabled, resetKey, placeholder, onHasValueChange }, ref) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const hasValueRef = useRef(false)
  const updateValue = useCallback((nextValue: string) => {
    setValue(nextValue)
    const nextHasValue = Boolean(nextValue.trim())
    if (nextHasValue !== hasValueRef.current) {
      hasValueRef.current = nextHasValue
      onHasValueChange(nextHasValue)
    }
  }, [onHasValueChange])
  useImperativeHandle(ref, () => ({ getValue: () => value, clear: () => updateValue(''), setValue: updateValue, focus: () => textareaRef.current?.focus() }), [value, updateValue])
  useEffect(() => {
    setValue('')
    if (hasValueRef.current) {
      hasValueRef.current = false
      onHasValueChange(false)
    }
  }, [resetKey, onHasValueChange])
  return <textarea ref={textareaRef} aria-label="给 Agent 发送消息" value={value} disabled={disabled} onChange={(event) => updateValue(event.target.value)} placeholder={placeholder} rows={2} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} />
})
