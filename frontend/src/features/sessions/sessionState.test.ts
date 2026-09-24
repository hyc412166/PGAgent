import { describe, expect, it } from 'vitest'

import { latestRunPlanFromEvents, orderDurableTasks, runPlanFromEvent } from './sessionState'
import type { DurableTask } from '../../types'

describe('当前运行计划事件', () => {
  it('同时读取实时事件和持久化事件的公开计划', () => {
    expect(runPlanFromEvent({
      type: 'plan_updated',
      plan: [{ id: 'inspect', content: '检查实现', status: 'in_progress' }],
    })).toEqual([{ id: 'inspect', content: '检查实现', status: 'in_progress' }])

    expect(runPlanFromEvent({
      event_type: 'plan_updated',
      payload: { plan: [{ id: 'test', content: '运行测试', status: 'pending' }] },
    })).toEqual([{ id: 'test', content: '运行测试', status: 'pending' }])
  })

  it('回放时使用最后一次计划更新并支持显式清空', () => {
    expect(latestRunPlanFromEvents([
      { event_type: 'plan_updated', payload: { plan: [{ id: 'first', content: '第一步', status: 'completed' }] } },
      { event_type: 'tool_finished', payload: { tool_name: 'read' } },
      { event_type: 'plan_updated', payload: { plan: [] } },
    ])).toEqual([])
  })

  it('忽略非计划事件和不合法步骤', () => {
    expect(runPlanFromEvent({ type: 'tool_finished' })).toBeNull()
    expect(runPlanFromEvent({
      type: 'plan_updated',
      plan: [
        { id: '', content: '缺少标识', status: 'pending' },
        { id: 'invalid', content: '未知状态', status: 'blocked' },
      ],
    })).toEqual([])
  })
})

describe('持久任务展示顺序', () => {
  it('将活动任务置顶，其余任务按创建时间和 id 稳定排列', () => {
    const tasks = [
      { id: 'task-3', status: 'completed', created_at: '2026-09-03T00:00:00Z' },
      { id: 'task-1', status: 'completed', created_at: '2026-09-01T00:00:00Z' },
      { id: 'task-2', status: 'running', created_at: '2026-09-02T00:00:00Z' },
    ] as DurableTask[]

    expect(orderDurableTasks(tasks).map((task) => task.id)).toEqual(['task-2', 'task-1', 'task-3'])
  })
})
