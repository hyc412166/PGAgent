import { describe, expect, it } from 'vitest'
import { buildDraftLaunchPayload, createDraftIdempotencyKey } from './draftLaunch'

describe('临时草稿原子启动', () => {
  it('为一个草稿生成可复用的稳定幂等键', () => {
    const key = createDraftIdempotencyKey(() => 'fixed-uuid')
    expect(key).toBe('draft-fixed-uuid')
    expect(buildDraftLaunchPayload(key, '  分析   当前项目  ', '', {
      model_connection_id: null,
      model_id: null,
      thinking_level: 'auto',
    })).toEqual({
      idempotency_key: key,
      title: '分析 当前项目',
      content: '分析   当前项目',
      thinking_level: 'auto',
    })
  })

  it('仅在选择项目或模型时发送可选字段', () => {
    expect(buildDraftLaunchPayload('draft-1', '执行任务', ' D:\\repo ', {
      model_connection_id: 'connection-1',
      model_id: 'model-1',
      thinking_level: 'high',
    })).toEqual({
      idempotency_key: 'draft-1',
      title: '执行任务',
      content: '执行任务',
      root_path: 'D:\\repo',
      model_connection_id: 'connection-1',
      model_id: 'model-1',
      thinking_level: 'high',
    })
  })
})
