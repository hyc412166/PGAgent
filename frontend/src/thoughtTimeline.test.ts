import { describe, expect, it } from 'vitest'
import { emptyThoughtTimeline, formatLiveThinkingDuration, formatThoughtDuration, hasVisibleCompletedThought, pickThinkingStatus, safeToolTarget, summarizeThoughtConclusion, thinkingStatusForRun, timelineFromRunEvents, updateThoughtTimeline } from './thoughtTimeline'

describe('实时 Thought 时间线', () => {
  it('累计思考耗时并跟踪工具的开始与完成', () => {
    const started = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_step_started' }, 1_000)
    const reading = updateThoughtTimeline(started, { type: 'tool_started', tool_name: 'read_file', tool_call_id: 'call-1', arguments: { path: '.gitignore', content: '不可展示' } }, 1_250)
    const read = updateThoughtTimeline(reading, { type: 'tool_finished', tool_call_id: 'call-1' }, 1_600)
    const completed = updateThoughtTimeline(read, { type: 'run_completed' }, 1_858)

    expect(completed.elapsedMs).toBe(858)
    expect(completed.finished).toBe(true)
    expect(completed.tools).toEqual([{ id: 'call-1', name: 'Read', target: '.gitignore', status: 'completed' }])
    expect(formatThoughtDuration(completed.elapsedMs)).toBe('858ms')
  })

  it('只提取路径或去掉查询参数的 URL，不显示其它工具参数', () => {
    const event = { type: 'tool_call', arguments: { url: 'https://example.com/docs/long?token=secret#private', api_key: 'never-show', content: 'never-show' } }
    expect(safeToolTarget(event)).toBe('https://example.com/docs/long')
    expect(JSON.stringify(safeToolTarget(event))).not.toContain('secret')
    expect(formatThoughtDuration(2_450)).toBe('2.5s')
  })

  it('为一次运行稳定选择幽默状态，并用秒显示实时耗时', () => {
    expect(pickThinkingStatus(() => 0)).toBe('翻抽屉找思路中… (•̀ᴗ•́)و')
    expect(thinkingStatusForRun('run-42')).toBe(thinkingStatusForRun('run-42'))
    expect(formatLiveThinkingDuration(2_345)).toBe('2.3 秒')
  })

  it('可以从持久化 RunEvent 重建刷新后仍可见的终态时间线', () => {
    const timeline = timelineFromRunEvents([
      { type: '', event_type: 'model_step_started', created_at: '2026-08-11T00:00:00.000Z', payload: {} },
      { type: '', event_type: 'tool_started', created_at: '2026-08-11T00:00:00.300Z', payload: { tool_name: 'webfetch', tool_call_id: 'web-1', url: 'https://example.com/docs?token=hidden' } },
      { type: '', event_type: 'tool_finished', created_at: '2026-08-11T00:00:00.700Z', payload: { tool_call_id: 'web-1', ok: true } },
      { type: '', event_type: 'run_completed', created_at: '2026-08-11T00:00:00.858Z', payload: { elapsed_ms: 858 } },
    ])
    expect(hasVisibleCompletedThought(timeline)).toBe(true)
    expect(timeline.elapsedMs).toBe(858)
    expect(timeline.tools[0]).toMatchObject({ name: 'WebFetch', target: 'https://example.com/docs', status: 'completed' })
    expect(timeline.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: 'thought', title: '思考', detail: '' }),
      expect.objectContaining({ kind: 'tool', title: 'WebFetch', icon: 'search', status: 'completed' }),
    ]))
  })

  it('只展示安全进度摘要，不把原始 reasoning 或敏感参数带入时间线', () => {
    const started = updateThoughtTimeline(emptyThoughtTimeline, {
      type: 'model_step_started',
      summary: '正在检查项目结构',
      reasoning: '这是不应直接展示的隐藏思维链',
      arguments: { api_key: 'secret-value' },
    }, 1_000)
    const activity = updateThoughtTimeline(started, {
      type: 'progress',
      progress: '已完成目录扫描',
      reasoning: '仍然不能展示',
    }, 1_100)
    expect(activity.items.map((item) => item.detail)).toEqual(['正在检查项目结构', '已完成目录扫描'])
    expect(JSON.stringify(activity.items)).not.toContain('隐藏思维链')
  })

  it('把实时思考增量连续追加到当前思考项，并由最终摘要收束', () => {
    const started = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_step_started', step: 1 }, 1_000)
    const first = updateThoughtTimeline(started, { type: 'thought_delta', step: 1, delta: '先检查' }, 1_050)
    const second = updateThoughtTimeline(first, { type: 'thought_delta', step: 1, delta: '天气来源。' }, 1_100)
    const finished = updateThoughtTimeline(second, { type: 'thought_summary', step: 1, summary: '先检查天气来源。', complete: true }, 1_150)

    expect(second.items).toEqual([
      expect.objectContaining({ id: 'thought-step-1', title: '思考', detail: '先检查天气来源。', status: 'running' }),
    ])
    expect(second.activeItemId).toBe('thought-step-1')
    expect(finished.items[0]).toMatchObject({ detail: '先检查天气来源。', status: 'completed' })
    expect(finished.activeItemId).toBeUndefined()
  })

  it('只把当前正在运行的步骤标记为呼吸灯目标', () => {
    const thinking = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_step_started', step: 1 }, 1_000)
    expect(thinking.activeItemId).toBe('thought-step-1')

    const tool = updateThoughtTimeline(thinking, { type: 'tool_started', tool_name: 'websearch', tool_call_id: 'web-1' }, 1_100)
    expect(tool.activeItemId).toBe('web-1')

    const finished = updateThoughtTimeline(tool, { type: 'tool_finished', tool_call_id: 'web-1', ok: true }, 1_200)
    expect(finished.activeItemId).toBeUndefined()

    const verifying = updateThoughtTimeline(finished, { type: 'completion_verification_started', event_id: 'verify-1' }, 1_300)
    expect(verifying.activeItemId).toBe('verify-1')
  })

  it('中断后用服务端快照补齐可能漏掉的最后一段思考', () => {
    const started = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_step_started', step: 2 }, 1_000)
    const partial = updateThoughtTimeline(started, { type: 'thought_delta', step: 2, delta: '正在分析' }, 1_100)
    const stopped = updateThoughtTimeline(partial, { type: 'run_stopped', partial_thought: '正在分析天气数据。' }, 1_200)

    expect(stopped.items[0]).toMatchObject({ detail: '正在分析天气数据。', status: 'completed' })
  })

  it('为默认收起的处理摘要提取安全、简短的结论首行', () => {
    expect(summarizeThoughtConclusion('## 最终结论\n\n已经完成配置。')).toBe('最终结论')
    expect(summarizeThoughtConclusion('')).toBe('任务已完成')
    expect(summarizeThoughtConclusion('这是一个很长的结论。'.repeat(20), 16)).toHaveLength(16)
  })
})
