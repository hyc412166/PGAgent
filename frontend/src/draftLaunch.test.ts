// 本测试文件验证 draftLaunch 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'
import { buildDraftLaunchPayload, createDraftIdempotencyKey, createTurnIdempotencyKey } from './draftLaunch'
import { emptyDraftSettings } from './features/sessions/sessionState'

// 测试分组：临时草稿原子启动。
describe('临时草稿原子启动', () => {
  // 测试场景：为一个草稿生成可复用的稳定幂等键。
  it('为一个草稿生成可复用的稳定幂等键', () => {
    const key = createDraftIdempotencyKey(() => 'fixed-uuid')
    expect(key).toBe('draft-fixed-uuid')
    expect(createTurnIdempotencyKey(() => 'fixed-turn')).toBe('turn-fixed-turn')
    expect(buildDraftLaunchPayload(key, '  分析   当前项目  ', '', {
      model_connection_id: null,
      model_id: null,
      thinking_level: 'medium',
      skill_ids: [],
      mcp_server_names: [],
      permission_mode: 'smart',
      use_memories: true,
    })).toEqual({
      idempotency_key: key,
      title: '分析 当前项目',
      content: '分析   当前项目',
      thinking_level: 'medium',
      skill_ids: [],
      mcp_server_names: [],
      permission_mode: 'smart',
      use_memories: true,
    })
  })

  // 测试场景：仅在选择项目或模型时发送可选字段。
  it('仅在选择项目或模型时发送可选字段', () => {
    expect(buildDraftLaunchPayload('draft-1', '执行任务', ' D:\\repo ', {
      model_connection_id: 'connection-1',
      model_id: 'model-1',
      thinking_level: 'high',
      skill_ids: ['skill-1', 'skill-1', 'skill-2'],
      mcp_server_names: ['filesystem', 'filesystem', 'github'],
      permission_mode: 'ask',
      use_memories: false,
    })).toEqual({
      idempotency_key: 'draft-1',
      title: '执行任务',
      content: '执行任务',
      root_path: 'D:\\repo',
      model_connection_id: 'connection-1',
      model_id: 'model-1',
      thinking_level: 'high',
      skill_ids: ['skill-1', 'skill-2'],
      mcp_server_names: ['filesystem', 'github'],
      permission_mode: 'ask',
      use_memories: false,
    })
  })

  // 测试场景：新草稿默认使用已有记忆。
  it('新草稿默认使用已有记忆', () => {
    expect(emptyDraftSettings.use_memories).toBe(true)
  })
})
