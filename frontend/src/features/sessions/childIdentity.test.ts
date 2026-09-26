import { describe, expect, it } from 'vitest'
import { childNames } from './childIdentity'
import { emptyThoughtTimeline, timelineFromRunEvents, updateThoughtTimeline } from '../../thoughtTimeline'

describe('委派身份与跳转关联', () => {
  const tasks = [
    { id: 'a', child_agent_id: 'coding', child_agent_name: 'Coding', title: '实现', created_at: '2026-09-01T00:00:00' },
    { id: 'b', child_agent_id: 'coding', child_agent_name: 'Coding', title: '测试', created_at: '2026-09-01T00:01:00' },
  ]
  it('编号不随列表倒序或状态改变', () => {
    expect(childNames(tasks)).toEqual({ a: 'Coding #1', b: 'Coding #2' })
    expect(childNames([...tasks].reverse().map(t => ({ ...t, status: 'completed' })))).toEqual(childNames(tasks))
  })
  it('优先展示主 Agent 指定的任务名称，旧记录仍使用编号', () => {
    expect(childNames([{ ...tasks[0], result: { display_name: '温度转换实现' } }, tasks[1]])).toEqual({ a: '温度转换实现', b: 'Coding #2' })
  })
  it('历史委派保留精确 ID，替代 task 工具行而保留完成状态', () => {
    const timeline = timelineFromRunEvents([
      { type: 'tool_started', tool_name: 'task', tool_call_id: 'call', sequence: 1 },
      { type: 'delegated_child_started', task_id: 'a', child_run_id: 'run-a', sequence: 2 },
      { type: 'delegated_child_completed', task_id: 'a', child_run_id: 'run-a', sequence: 3 },
    ])
    expect(timeline.items.filter(i => i.delegationTool)).toHaveLength(0)
    const activities = timeline.items.filter(i => i.kind === 'task')
    expect(activities.map(i => i.delegationState)).toEqual(['开始工作', '已完成'])
    expect(activities[0].id).not.toBe(activities[1].id)
    for (const activity of activities) {
      expect(activity).toMatchObject({ delegationId: 'a', childRunId: 'run-a' })
      expect(childNames(tasks)[activity.delegationId!]).toBe('Coding #1')
    }
  })
  it('主 Agent 汇总事件不冒充子 Agent 启动', () => {
    const timeline = timelineFromRunEvents([{ type: 'delegated_child_continuation_started', tool_call_id: 'call', sequence: 1 }])
    expect(timeline.items).toHaveLength(1)
    expect(timeline.items[0]).toMatchObject({ kind: 'event', title: '主 Agent 汇总子 Agent 结果' })
    expect(timeline.items[0].delegationState).toBeUndefined()
  })
  it('实时事件重放去重，开始记录不被等待审批和终态覆盖', () => {
    let timeline = emptyThoughtTimeline
    for (const type of ['delegated_child_started', 'delegated_child_started', 'delegated_child_awaiting_approval', 'delegated_child_completed', 'delegated_child_completed']) {
      timeline = updateThoughtTimeline(timeline, { type, task_id: 'a', child_run_id: 'run-a' }, 1000)
    }
    expect(timeline.items.filter(i => i.kind === 'task').map(i => i.delegationState)).toEqual(['开始工作', '已完成'])
  })
})
