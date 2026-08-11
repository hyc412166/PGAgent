import { describe, expect, it } from 'vitest'
import { availableConnectionModels, chooseAutomaticConnection, resolveEffectiveModelSettings } from './modelSettings'

describe('会话模型自动回退', () => {
  it('优先选择健康且最近检查的启用连接', () => {
    const selected = chooseAutomaticConnection([
      { id: 'disabled', name: '禁用', enabled: false, status: 'healthy' },
      { id: 'older', name: '旧连接', enabled: true, status: 'connected', last_checked_at: '2026-08-01T00:00:00Z' },
      { id: 'newer', name: '新连接', enabled: true, status: 'healthy', last_checked_at: '2026-08-10T00:00:00Z' },
      { id: 'unchecked', name: '未检查', enabled: true, status: 'unknown' },
    ])
    expect(selected?.id).toBe('newer')
  })

  it('没有健康连接时至少返回首个启用连接，并优先显示默认模型', () => {
    const selected = chooseAutomaticConnection([
      { id: 'disabled', name: '禁用', enabled: false },
      { id: 'fallback', name: '回退', enabled: true, models: ['model-b'], default_model: 'model-a' },
    ])
    expect(selected?.id).toBe('fallback')
    expect(availableConnectionModels(selected)).toEqual(['model-a', 'model-b'])
  })

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

  it('当前显式模型与清除覆盖后的自动模型分别按后端规则解析', () => {
    const resolved = resolveEffectiveModelSettings(
      [
        { id: 'agent', name: 'Agent 连接', enabled: true, status: 'healthy', default_model: 'agent-default' },
        { id: 'session', name: '会话连接', enabled: true, status: 'online', default_model: 'session-default' },
      ],
      { id: 'session-row', model_connection_id: 'session', model_id: 'session-model' },
      { id: 'agent-row', name: 'Agent', model_connection_id: 'agent', model_id: 'agent-model' },
    )
    expect(resolved.model).toBe('session-model')
    expect(resolved.selectedValue).toBe('session::session-model')
    expect(resolved.automaticConnection?.id).toBe('agent')
    expect(resolved.automaticModel).toBe('agent-model')
  })
})
