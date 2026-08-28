import { describe, expect, it } from 'vitest'
import { buildDraftLaunchPayload, createDraftIdempotencyKey, createTurnIdempotencyKey } from './draftLaunch'
import { emptyDraftSettings } from './features/sessions/sessionState'

describe('临时草稿原子启动', () => {
  it('为一个草稿生成可复用的稳定幂等键', () => {
    const key = createDraftIdempotencyKey(() => 'fixed-uuid')
    expect(key).toBe('draft-fixed-uuid')
    expect(createTurnIdempotencyKey(() => 'fixed-turn')).toBe('turn-fixed-turn')
    expect(buildDraftLaunchPayload(key, '  分析   当前项目  ', '', {
      model_connection_id: null,
      model_id: null,
      thinking_level: 'auto',
      skill_ids: [],
      permission_mode: 'smart',
      use_memories: true,
    })).toEqual({
      idempotency_key: key,
      title: '分析 当前项目',
      content: '分析   当前项目',
      thinking_level: 'auto',
      skill_ids: [],
      permission_mode: 'smart',
      use_memories: true,
    })
  })

  it('仅在选择项目或模型时发送可选字段', () => {
    expect(buildDraftLaunchPayload('draft-1', '执行任务', ' D:\\repo ', {
      model_connection_id: 'connection-1',
      model_id: 'model-1',
      thinking_level: 'high',
      skill_ids: ['skill-1', 'skill-1', 'skill-2'],
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
      permission_mode: 'ask',
      use_memories: false,
    })
  })

  it('新草稿默认使用已有记忆', () => {
    expect(emptyDraftSettings.use_memories).toBe(true)
  })
})
