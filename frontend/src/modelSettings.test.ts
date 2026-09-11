// 本测试文件验证 modelSettings 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'
import { allConnectionModels, availableConnectionModels, chooseAutomaticConnection, resolveEffectiveModelSettings } from './modelSettings'

// 测试分组：会话模型自动回退。
describe('会话模型自动回退', () => {
  // 测试场景：优先选择健康且最近检查的启用连接。
  it('优先选择健康且最近检查的启用连接', () => {
    const selected = chooseAutomaticConnection([
      { id: 'disabled', name: '禁用', enabled: false, status: 'healthy' },
      { id: 'older', name: '旧连接', enabled: true, status: 'connected', default_model: 'older-model', last_checked_at: '2026-08-01T00:00:00Z' },
      { id: 'newer', name: '新连接', enabled: true, status: 'healthy', default_model: 'newer-model', last_checked_at: '2026-08-10T00:00:00Z' },
      { id: 'unchecked', name: '未检查', enabled: true, status: 'unknown', default_model: 'unchecked-model' },
    ])
    expect(selected?.id).toBe('newer')
  })

  // 测试场景：没有健康连接时至少返回首个启用连接，并优先显示默认模型。
  it('没有健康连接时至少返回首个启用连接，并优先显示默认模型', () => {
    const selected = chooseAutomaticConnection([
      { id: 'disabled', name: '禁用', enabled: false },
      { id: 'fallback', name: '回退', enabled: true, models: ['model-b'], default_model: 'model-a' },
    ])
    expect(selected?.id).toBe('fallback')
    expect(availableConnectionModels(selected)).toEqual(['model-b', 'model-a'])
  })

  // 测试场景：切换启用状态导致默认模型变化时，完整目录仍保持发现顺序。
  it('默认模型变化不改变模型目录顺序', () => {
    const discoveredModels = ['codex-auto-review', 'gpt-5.3-codex', 'gpt-5.6-luna']

    expect(allConnectionModels({
      id: 'connection',
      name: '连接',
      default_model: 'codex-auto-review',
      discovered_models: discoveredModels,
    })).toEqual(discoveredModels)
    expect(allConnectionModels({
      id: 'connection',
      name: '连接',
      default_model: 'gpt-5.6-luna',
      discovered_models: discoveredModels,
      disabled_models: ['codex-auto-review'],
    })).toEqual(discoveredModels)
  })

  // 测试场景：自动连接不选择模型已全部停用的连接。
  it('跳过没有启用模型的连接', () => {
    const selected = chooseAutomaticConnection([
      { id: 'empty', name: '空连接', enabled: true, status: 'healthy', default_model: 'model-a', disabled_models: ['model-a'] },
      { id: 'usable', name: '可用连接', enabled: true, status: 'connected', default_model: 'model-b' },
    ])

    expect(selected?.id).toBe('usable')
  })

  // 测试场景：模型设置页保留完整目录，但会话选择器只返回当前启用的模型。
  it('从会话可用模型中排除已禁用项', () => {
    const connection = {
      id: 'filtered',
      name: '筛选连接',
      default_model: 'model-a',
      discovered_models: ['model-a', 'model-b', 'model-c'],
      disabled_models: ['model-a', 'model-c'],
    }

    expect(allConnectionModels(connection)).toEqual(['model-a', 'model-b', 'model-c'])
    expect(availableConnectionModels(connection)).toEqual(['model-b'])
  })

  // 测试场景：忽略会话中已经禁用的连接和不属于回退连接的旧模型。
  it('忽略会话中已经禁用的连接和不属于回退连接的旧模型', () => {
    const resolved = resolveEffectiveModelSettings(
      [
        { id: 'disabled', name: '已禁用', enabled: false, default_model: 'stale-model' },
        { id: 'healthy', name: '健康连接', enabled: true, status: 'healthy', default_model: 'live-model' },
      ],
      { id: 'session', model_connection_id: 'disabled', model_id: 'stale-model' },
    )
    expect(resolved.connectionId).toBe('healthy')
    expect(resolved.model).toBe('live-model')
    expect(resolved.selectedValue).toBe('')
  })

  // 测试场景：当前显式模型与清除覆盖后的自动模型分别按后端规则解析。
  it('当前显式模型与清除覆盖后的自动模型分别按后端规则解析', () => {
    const resolved = resolveEffectiveModelSettings(
      [
        { id: 'agent', name: 'Agent 连接', enabled: true, status: 'healthy', default_model: 'agent-default', discovered_models: ['agent-default', 'agent-model'] },
        { id: 'session', name: '会话连接', enabled: true, status: 'online', default_model: 'session-default', discovered_models: ['session-default', 'session-model'] },
      ],
      { id: 'session-row', model_connection_id: 'session', model_id: 'session-model' },
      { id: 'agent-row', name: 'Agent', model_connection_id: 'agent', model_id: 'agent-model' },
    )
    expect(resolved.model).toBe('session-model')
    expect(resolved.selectedValue).toBe('session::session-model')
    expect(resolved.automaticConnection?.id).toBe('agent')
    expect(resolved.automaticModel).toBe('agent-model')
  })

  // 测试场景：会话保存的模型已被禁用时不再展示或沿用，改用同连接的首个启用模型。
  it('忽略会话中已经禁用的模型', () => {
    const resolved = resolveEffectiveModelSettings(
      [{
        id: 'connection',
        name: '连接',
        enabled: true,
        status: 'healthy',
        default_model: 'model-b',
        discovered_models: ['model-a', 'model-b'],
        disabled_models: ['model-a'],
      }],
      { id: 'session', model_connection_id: 'connection', model_id: 'model-a' },
    )

    expect(resolved.model).toBe('model-b')
    expect(resolved.selectedValue).toBe('connection::model-b')
  })

  // 测试场景：默认模型不在展示目录首位时，自动选择仍优先采用有效默认模型。
  it('展示顺序固定后仍优先使用默认模型', () => {
    const resolved = resolveEffectiveModelSettings([{
      id: 'connection',
      name: '连接',
      enabled: true,
      status: 'healthy',
      default_model: 'gpt-5.6-luna',
      discovered_models: ['codex-auto-review', 'gpt-5.3-codex', 'gpt-5.6-luna'],
      disabled_models: ['codex-auto-review'],
    }])

    expect(resolved.model).toBe('gpt-5.6-luna')
    expect(resolved.automaticModel).toBe('gpt-5.6-luna')
  })
})
