// 本测试文件验证 thoughtTimeline 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'
import { displayToolName, emptyThoughtTimeline, formatLiveThinkingDuration, formatThoughtDuration, hasVisibleCompletedThought, pickThinkingStatus, safeToolTarget, summarizeThoughtConclusion, thinkingStatusForRun, timelineFromRunEvents, updateThoughtTimeline } from './thoughtTimeline'

// 测试分组：实时 Thought 时间线。
describe('实时 Thought 时间线', () => {
  // 测试场景：展示规范工具名并兼容旧会话别名。
  it('展示规范工具名并兼容旧会话别名', () => {
    expect(displayToolName('shell')).toBe('Shell')
    expect(displayToolName('bash')).toBe('Shell')
    expect(displayToolName('web_search')).toBe('WebSearch')
    expect(displayToolName('web_open')).toBe('Web Open')
    expect(displayToolName('update_plan')).toBe('Update Plan')
    expect(displayToolName('todowrite')).toBe('Update Plan')
    expect(displayToolName('tool_search')).toBe('Tool Search')
    expect(displayToolName('web_run')).toBe('联网搜索')
  })

  // 测试场景：累计思考耗时并跟踪工具的开始与完成。
  it('累计思考耗时并跟踪工具的开始与完成', () => {
    const started = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_step_started' }, 1_000)
    const reading = updateThoughtTimeline(started, { type: 'tool_started', tool_name: 'read_file', tool_call_id: 'call-1', arguments: { path: '.gitignore', content: '不可展示' } }, 1_250)
    const read = updateThoughtTimeline(reading, { type: 'tool_finished', tool_call_id: 'call-1' }, 1_600)
    const completed = updateThoughtTimeline(read, { type: 'run_completed' }, 1_858)

    expect(completed.elapsedMs).toBe(858)
    expect(completed.finished).toBe(true)
    expect(completed.tools).toEqual([{ id: 'call-1', name: 'Read', target: '.gitignore', status: 'completed' }])
    expect(formatThoughtDuration(completed.elapsedMs)).toBe('858ms')
    expect(completed.items.find((item) => item.kind === 'tool')?.detail).toContain('读取：.gitignore')
  })

  it('展示 rg/read 的具体参数并隐藏内部 tool_search', () => {
    const searched = updateThoughtTimeline(emptyThoughtTimeline, {
      type: 'tool_started', tool_name: 'rg', tool_call_id: 'rg-1', arguments: { pattern: 'web_run', path: 'backend/src' },
    }, 1_000)
    const hidden = updateThoughtTimeline(searched, { type: 'tool_started', tool_name: 'tool_search', tool_call_id: 'internal-1', arguments: { query: 'select:web_search' } }, 1_100)
    expect(hidden.items).toHaveLength(1)
    expect(hidden.items[0].detail).toBe('匹配：web_run · 路径：backend/src')
  })

  it('展示 read_artifact 的具体 artifact 标识', () => {
    const state = updateThoughtTimeline(emptyThoughtTimeline, {
      type: 'tool_started', tool_name: 'read_artifact', tool_call_id: 'artifact-1', arguments: { artifact_id: 'artifact_abc123' },
    }, 1_000)
    expect(state.items[0].detail).toBe('读取：artifact_abc123')
  })

  // 测试场景：只提取路径或去掉查询参数的 URL，不显示其它工具参数。
  it('只提取路径或去掉查询参数的 URL，不显示其它工具参数', () => {
    const event = { type: 'tool_call', arguments: { url: 'https://example.com/docs/long?token=secret#private', api_key: 'never-show', content: 'never-show' } }
    expect(safeToolTarget(event)).toBe('https://example.com/docs/long')
    expect(JSON.stringify(safeToolTarget(event))).not.toContain('secret')
    expect(formatThoughtDuration(2_450)).toBe('2s')
    expect(formatThoughtDuration(14 * 60_000 + 28_000)).toBe('14m 28s')
  })

  // 测试场景：为一次运行稳定选择幽默状态，并用秒显示实时耗时。
  it('为一次运行稳定选择幽默状态，并用秒显示实时耗时', () => {
    expect(pickThinkingStatus(() => 0)).toBe('翻抽屉找思路中… (•̀ᴗ•́)و')
    expect(thinkingStatusForRun('run-42')).toBe(thinkingStatusForRun('run-42'))
    expect(formatLiveThinkingDuration(2_345)).toBe('2 秒')
  })

  // 测试场景：可以从持久化 RunEvent 重建刷新后仍可见的终态时间线。
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

  // 测试场景：只展示模型主动输出的安全进度，不展示后端固定 progress。
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

  // 测试场景：隐藏 provider reasoning，只保留模型可见阶段进度。
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

  // 测试场景：只把当前正在运行的步骤标记为呼吸灯目标。
  it('只把当前正在运行的步骤标记为呼吸灯目标', () => {
    const thinking = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_step_started', step: 1 }, 1_000)
    expect(thinking.activeItemId).toBe('thought-step-1')

    const tool = updateThoughtTimeline(thinking, { type: 'tool_started', tool_name: 'websearch', tool_call_id: 'web-1' }, 1_100)
    expect(tool.activeItemId).toBe('web-1')

    const finished = updateThoughtTimeline(tool, { type: 'tool_finished', tool_call_id: 'web-1', ok: true }, 1_200)
    expect(finished.activeItemId).toBeUndefined()

    const verifying = updateThoughtTimeline(finished, { type: 'completion_verification_started', event_id: 'verify-1' }, 1_300)
    expect(verifying.activeItemId).toBeUndefined()
    expect(verifying.items).toHaveLength(2)
  })

  // 测试场景：工具完成事件不带参数时，不能覆盖开始阶段记录的具体命令。
  it('工具完成后保留具体调用详情', () => {
    const started = updateThoughtTimeline(emptyThoughtTimeline, {
      type: 'tool_started', tool_name: 'shell', tool_call_id: 'shell-1',
      arguments: { command: { text: 'rg -n web_run backend/src', executable: 'rg', argument_count: 3 } },
    }, 1_000)
    const finished = updateThoughtTimeline(started, {
      type: 'tool_finished', tool_name: 'shell', tool_call_id: 'shell-1', ok: true,
      result_summary: '命中 5 行',
    }, 1_100)

    expect(finished.items[0]).toMatchObject({
      title: 'Shell',
      detail: '执行 rg（3 个参数） · 命中 5 行',
      status: 'completed',
    })
  })

  // 测试场景：联网搜索的长网址仍保持单行详情，天气调用显示具体地点并追加来源地址。
  it('展示长网址和天气来源详情而不退回通用文案', () => {
    const longUrl = 'https://example.com/technology/' + 'very-long-article-slug-'.repeat(12)
    const opened = updateThoughtTimeline(emptyThoughtTimeline, {
      type: 'tool_started', tool_name: 'web_run', tool_call_id: 'web-open-1',
      arguments: { open: [{ url: longUrl }] },
    }, 1_000)
    expect(opened.items[0]).toMatchObject({ title: '联网搜索', detail: `打开：${longUrl}` })

    const weather = updateThoughtTimeline(emptyThoughtTimeline, {
      type: 'tool_started', tool_name: 'web_run', tool_call_id: 'weather-1',
      arguments: { weather: { count: 1, items: ['New York, NY'] } },
    }, 2_000)
    const finished = updateThoughtTimeline(weather, {
      type: 'tool_finished', tool_name: 'web_run', tool_call_id: 'weather-1', ok: true,
      result_summary: '来源：https://api.open-meteo.com/v1/forecast',
    }, 2_100)
    expect(finished.items[0]).toMatchObject({
      title: '联网搜索',
      detail: '天气：New York, NY · 来源：https://api.open-meteo.com/v1/forecast',
    })
  })

  // 测试场景：分别展示 MCP 目录准备和按需连接进度。
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

  // 测试场景：中断后保留已经流式到达的思考片段。
  it('中断后保留已经流式到达的思考片段', () => {
    const started = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_step_started', step: 2 }, 1_000)
    const partial = updateThoughtTimeline(started, { type: 'thought_delta', step: 2, delta: '正在分析' }, 1_100)
    const stopped = updateThoughtTimeline(partial, { type: 'run_stopped', partial_thought: '正在分析天气数据。' }, 1_200)

    expect(stopped.items[0]).toMatchObject({ title: '思考', detail: '正在分析', status: 'completed' })
  })

  // 测试场景：重放未知持久事件时不把任意 payload 变成可见时间线内容。
  it('忽略未知持久事件中的任意 payload', () => {
    const timeline = timelineFromRunEvents([{
      type: '',
      event_type: 'provider_internal_packet',
      created_at: '2026-09-08T08:00:00Z',
      payload: { summary: 'secret-summary', reason: 'secret-reason', raw: { token: 'secret-token' } },
    }])

    expect(timeline).toEqual(emptyThoughtTimeline)
    expect(JSON.stringify(timeline)).not.toContain('secret')
  })

  // 测试场景：后续模型重试覆盖当前重试行。
  it('后续模型重试覆盖当前重试行', () => {
    const first = updateThoughtTimeline(emptyThoughtTimeline, { type: 'model_retry', attempt: 1, event_id: 'retry-1' }, 1_000)
    const second = updateThoughtTimeline(first, { type: 'model_retry', attempt: 2, event_id: 'retry-2' }, 1_100)

    expect(second.items).toEqual([
      expect.objectContaining({ id: 'model-retry', title: '重试模型', detail: '第 2 次重试' }),
    ])
  })

  // 测试场景：为默认收起的处理摘要提取安全、简短的结论首行。
  it('为默认收起的处理摘要提取安全、简短的结论首行', () => {
    expect(summarizeThoughtConclusion('## 最终结论\n\n已经完成配置。')).toBe('最终结论')
    expect(summarizeThoughtConclusion('')).toBe('任务已完成')
    expect(summarizeThoughtConclusion('这是一个很长的结论。'.repeat(20), 16)).toHaveLength(16)
  })
})
