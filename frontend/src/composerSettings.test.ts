// 本测试文件验证 composerSettings 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'
import { modelSelectionPayload, resolveEffectiveThinking, shortModelLabel, thinkingLevelLabels } from './composerSettings'

// 测试分组：会话组合设置菜单。
describe('会话组合设置菜单', () => {
  // 测试场景：自动思考会逐层继承实际强度。
  it('自动思考会逐层继承实际强度', () => {
    expect(resolveEffectiveThinking('auto', 'auto', 'high')).toBe('high')
    expect(thinkingLevelLabels[resolveEffectiveThinking('auto', 'low', 'high')]).toBe('低')
    expect(resolveEffectiveThinking('auto', 'off', 'auto')).toBe('medium')
  })

  // 测试场景：模型按钮使用短名但保留可识别信息。
  it('模型按钮使用短名但保留可识别信息', () => {
    expect(shortModelLabel('openrouter/openai/gpt-5.6-sol')).toBe('gpt-5.6-sol')
    expect(shortModelLabel('vendor/a-very-long-model-identifier', 12)).toBe('a-very-long…')
  })

  // 测试场景：模型选择值转换为 PATCH payload，自动项清空覆盖。
  it('模型选择值转换为 PATCH payload，自动项清空覆盖', () => {
    expect(modelSelectionPayload('connection-1::deepseek-chat')).toEqual({ model_connection_id: 'connection-1', model_id: 'deepseek-chat' })
    expect(modelSelectionPayload('')).toEqual({ model_connection_id: null, model_id: null })
  })
})
