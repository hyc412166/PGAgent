import { describe, expect, it } from 'vitest'
import { modelSelectionPayload, resolveEffectiveThinking, shortModelLabel, thinkingLevelLabels } from './composerSettings'

describe('会话组合设置菜单', () => {
  it('自动思考会逐层继承实际强度', () => {
    expect(resolveEffectiveThinking('auto', 'auto', 'high')).toBe('high')
    expect(thinkingLevelLabels[resolveEffectiveThinking('auto', 'low', 'high')]).toBe('低')
    expect(resolveEffectiveThinking('auto', 'off', 'auto')).toBe('medium')
  })

  it('模型按钮使用短名但保留可识别信息', () => {
    expect(shortModelLabel('openrouter/openai/gpt-5.6-sol')).toBe('gpt-5.6-sol')
    expect(shortModelLabel('vendor/a-very-long-model-identifier', 12)).toBe('a-very-long…')
  })

  it('模型选择值转换为 PATCH payload，自动项清空覆盖', () => {
    expect(modelSelectionPayload('connection-1::deepseek-chat')).toEqual({ model_connection_id: 'connection-1', model_id: 'deepseek-chat' })
    expect(modelSelectionPayload('')).toEqual({ model_connection_id: null, model_id: null })
  })
})
