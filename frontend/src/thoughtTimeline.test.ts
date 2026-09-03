import { describe, expect, it } from 'vitest'
import { displayToolName, emptyThoughtTimeline, formatLiveThinkingDuration, formatThoughtDuration, hasVisibleCompletedThought, pickThinkingStatus, safeToolTarget, summarizeThoughtConclusion, thinkingStatusForRun, timelineFromRunEvents, updateThoughtTimeline } from './thoughtTimeline'

describe('实时 Thought 时间线', () => {
  it('展示规范工具名并兼容旧会话别名', () => {
    expect(displayToolName('shell')).toBe('Shell')
    expect(displayToolName('bash')).toBe('Shell')
    expect(displayToolName('web_search')).toBe('WebSearch')
    expect(displayToolName('web_open')).toBe('Web Open')
    expect(displayToolName('update_plan')).toBe('Update Plan')
    expect(displayToolName('todowrite')).toBe('Update Plan')
    expect(displayToolName('tool_search')).toBe('Tool Search')
  })

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

  it('只展示模型主动输出的安全进度，不展示后端固定 progress', () => {
    const started = updateThoughtTimeline(emptyThoughtTimeline, {
      type: 'model_step_started',
      summary: '正在检查项目结构',
      reasoning: '这是不应直接展示的隐藏思维链',
      arguments: { api_key: 'secret-value' },
    }, 1_000)
    const activity = updateThoughtTimeline(started, {
      type: 'thought_summary',
      summary: '已完成目录扫描',
      reasoning: '仍然不能展示',
    }, 1_100)
    const ignored = updateThoughtTimeline(activity, {
      type: 'progress',
      progress: '后端固定播报',
    }, 1_200)
    expect(ignored.items.map((item) => item.detail)).toEqual(['已完成目录扫描'])
    expect(JSON.stringify(activity.items)).not.toContain('隐藏思维链')
  })

  it('隐藏 provider reasoning，只保留模型可见阶段进度', () => {
    const started = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_step_started', step: 1 }, 1_000)
    const first = updateThoughtTimeline(started, { type: 'thought_delta', step: 1, delta: '先检查' }, 1_050)
    const providerSummary = updateThoughtTimeline(first, { type: 'thought_summary', step: 1, summary: 'Planning weather retrieval', complete: true }, 1_100)
    const visible = updateThoughtTimeline(providerSummary, { type: 'thought_summary', summary: '我先检查天气来源，再核对发布时间。' }, 1_150)

    expect(visible.items).toEqual([
      expect.objectContaining({ id: 'thought-step-1', title: '思考', detail: '我先检查天气来源，再核对发布时间。', status: 'running' }),
    ])
    expect(JSON.stringify(visible.items)).not.toContain('Planning weather retrieval')
    expect(visible.activeItemId).toBe('thought-step-1')
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

  it('分别展示 MCP 目录准备和按需连接进度', () => {
    const loading = updateThoughtTimeline(emptyThoughtTimeline, {
      type: 'mcp_catalog_loading',
      servers: ['playwright'],
    }, 1_000)
    const ready = updateThoughtTimeline(loading, {
      type: 'mcp_ready',
      tool_count: 24,
      failed_servers: [],
    }, 15_000)

    expect(loading.items).toEqual([
      expect.objectContaining({
        id: 'mcp-catalog',
        title: '正在准备 MCP 工具目录',
        detail: 'playwright',
        status: 'running',
      }),
    ])
    expect(loading.activeItemId).toBe('mcp-catalog')
    expect(ready.items).toEqual([
      expect.objectContaining({
        id: 'mcp-catalog',
        title: 'MCP 已就绪',
        detail: '已发现 24 个工具',
        status: 'completed',
      }),
    ])
    expect(ready.activeItemId).toBeUndefined()

    const connecting = updateThoughtTimeline(ready, {
      type: 'mcp_connecting',
      servers: ['playwright'],
      trigger: 'tool_call',
    }, 16_000)
    const connected = updateThoughtTimeline(connecting, {
      type: 'mcp_server_ready',
      server: 'playwright',
    }, 17_000)
    expect(connecting.activeItemId).toBe('mcp-server-playwright')
    expect(connected.items.at(-1)).toMatchObject({
      id: 'mcp-server-playwright',
      title: 'MCP 服务已按需连接',
      status: 'completed',
    })
    expect(connected.activeItemId).toBeUndefined()
  })

  it('中断后不显示服务端保存的 provider reasoning 片段', () => {
    const started = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_step_started', step: 2 }, 1_000)
    const partial = updateThoughtTimeline(started, { type: 'thought_delta', step: 2, delta: '正在分析' }, 1_100)
    const stopped = updateThoughtTimeline(partial, { type: 'run_stopped', partial_thought: '正在分析天气数据。' }, 1_200)

    expect(stopped.items[0]).toMatchObject({ title: '思考', detail: '', status: 'completed' })
  })

  it('后续模型重试覆盖当前重试行', () => {
    const first = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_retry', attempt: 1, event_id: 'retry-1' }, 1_000)
    const second = updateThoughtTimeline(first, { type: 'model_retry', attempt: 2, event_id: 'retry-2' }, 1_100)

    expect(second.items).toEqual([
      expect.objectContaining({ id: 'model-retry', title: '重试模型', detail: '第 2 次重试' }),
    ])
  })

  it('为默认收起的处理摘要提取安全、简短的结论首行', () => {
    expect(summarizeThoughtConclusion('## 最终结论\n\n已经完成配置。')).toBe('最终结论')
    expect(summarizeThoughtConclusion('')).toBe('任务已完成')
    expect(summarizeThoughtConclusion('这是一个很长的结论。'.repeat(20), 16)).toHaveLength(16)
  })
})
