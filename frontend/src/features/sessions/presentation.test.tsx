// 本测试文件验证会话侧栏中的持久运行事件只使用安全展示模型。
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { ChildAgentPanel, CompletedThoughtTimeline, LiveAssistantMessage, MessageBubble } from './presentation'
import { groupThoughtActivities } from './thoughtActivityGrouping'

describe('子 Agent 运行事件展示', () => {
  // 测试场景：工具事件显示可读标题，未知事件也不会回显内部事件名或 payload。
  it('复用安全事件描述而不是原始事件类型', () => {
    const markup = renderToStaticMarkup(createElement(ChildAgentPanel, {
      open: true,
      tasks: [{ id: 'task-1', title: '检查日志', status: 'running', child_run_id: 'run-1' }],
      teammates: [],
      loading: false,
      error: '',
      selectedTask: { id: 'task-1', title: '检查日志', status: 'running', child_run_id: 'run-1' },
      run: { id: 'run-1' },
      events: [
        { id: 'event-1', event_type: 'tool_started', payload: { tool_name: 'Shell', arguments: { command: 'secret-command' } } },
        { id: 'event-2', event_type: 'provider_internal_packet', payload: { raw: 'secret-payload' } },
      ],
      eventsLoading: false,
      eventsError: '',
      onClose: vi.fn(),
      onSelect: vi.fn(),
      onRetry: vi.fn(),
    }))

    expect(markup).toContain('开始调用 Shell')
    expect(markup).not.toContain('tool_started')
    expect(markup).not.toContain('provider_internal_packet')
    expect(markup).not.toContain('secret-command')
    expect(markup).not.toContain('secret-payload')
  })
})

describe('助手消息中的思考过程展示', () => {
  it('只把相邻的多个工具调用合并成一个工具组', () => {
    const grouped = groupThoughtActivities([
      { id: 'thought-1', kind: 'thought', icon: 'think', title: '思考', detail: '先检查文件', status: 'completed' },
      { id: 'tool-1', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'rg -n first', status: 'completed' },
      { id: 'tool-2', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'Get-Content second', status: 'completed' },
      { id: 'progress-1', kind: 'event', icon: 'think', title: '进度', detail: '开始分析', status: 'completed' },
      { id: 'tool-3', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'pnpm test', status: 'completed' },
    ])

    expect(grouped).toHaveLength(4)
    expect(grouped[1]).toMatchObject({ kind: 'tool-group', id: 'tool-group:tool-1:tool-2' })
    expect(grouped[1].kind === 'tool-group' ? grouped[1].items.map((item) => item.id) : []).toEqual(['tool-1', 'tool-2'])
    expect(grouped[3]).toMatchObject({ kind: 'activity', item: expect.objectContaining({ id: 'tool-3' }) })
  })

  it('忽略没有可见文本的模型步骤标记，保持连续工具调用分组', () => {
    const grouped = groupThoughtActivities([
      { id: 'shell-1', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'Get-Location', status: 'completed' },
      { id: 'model-step-1', kind: 'thought', icon: 'think', title: '思考', detail: '', status: 'completed' },
      { id: 'shell-2', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'Get-Date', status: 'completed' },
      { id: 'model-step-2', kind: 'thought', icon: 'think', title: '思考', detail: '', status: 'completed' },
      { id: 'shell-3', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'Get-ChildItem', status: 'completed' },
    ])

    expect(grouped).toHaveLength(1)
    expect(grouped[0]).toMatchObject({ kind: 'tool-group' })
    expect(grouped[0].kind === 'tool-group' ? grouped[0].items.map((item) => item.id) : []).toEqual(['shell-1', 'shell-2', 'shell-3'])
  })

  it('连续 Shell 调用默认折叠为运行了命令', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-shell-group',
        phase: '执行中',
        draft: '',
        status: 'live',
        error: '',
        thinkingStatus: '处理中',
        thought: {
          startedAt: 1_000,
          elapsedMs: 0,
          finished: false,
          conclusion: '',
          tools: [],
          items: [
            { id: 'thought-1', kind: 'thought', icon: 'think', title: '思考', detail: '检查项目', status: 'completed' },
            { id: 'shell-1', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'Get-Content first.ts', status: 'completed' },
            { id: 'shell-2', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'rg -n second', status: 'completed' },
            { id: 'shell-3', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'pnpm test', status: 'completed' },
          ],
        },
      },
    }))

    expect(markup).toContain('运行了命令')
    expect(markup).toContain('tool-activity-group-toggle')
    expect(markup).toContain('aria-expanded="false"')
    expect(markup).not.toContain('Get-Content first.ts')
    expect(markup).not.toContain('rg -n second')
    expect(markup).not.toContain('pnpm test')
  })

  it('将助手 Markdown 代码块渲染为带语言标签和语法高亮的代码面板', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-markdown-code',
        role: 'assistant',
        content: '```python\ndef greet(name):\n    return f"Hello {name}"\n```',
        created_at: '2026-09-08T00:00:00.000Z',
      },
    }))

    expect(markup).toContain('markdown-code-block')
    expect(markup).toContain('Python')
    expect(markup).toContain('复制 Python 代码')
    expect(markup).toContain('hljs-keyword')
    expect(markup).toContain('def')
  })

  it('将助手 GFM 表格渲染为可滚动的语义表格', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-markdown-table',
        role: 'assistant',
        content: '| 测试规模 | 输入 Token | 输出 Token |\n| --- | ---: | ---: |\n| 单个简单任务 | 3万-8万 | 3千-1万 |',
        created_at: '2026-09-08T00:00:00.000Z',
      },
    }))

    expect(markup).toContain('<table>')
    expect(markup).toContain('<th>测试规模</th>')
    expect(markup).toContain('>3千-1万</td>')
    expect(markup).toContain('markdown-table-wrap')
  })

  it('实时助手草稿复用同一套 Markdown 代码块渲染', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-markdown-draft',
        phase: '执行中',
        draft: '```json\n{"ready": true}\n```',
        status: 'live',
        error: '',
        thinkingStatus: '处理中',
        thought: { startedAt: null, elapsedMs: 0, finished: false, conclusion: '', tools: [], items: [] },
      },
    }))

    expect(markup).toContain('markdown-code-block')
    expect(markup).toContain('JSON')
  })

  it('已经结束的实时思考首次渲染时默认收起详情', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-terminal-thought',
        phase: '已完成',
        draft: '任务已完成',
        status: 'terminal',
        error: '',
        thinkingStatus: '处理中',
        thought: {
          startedAt: 1_000,
          elapsedMs: 4_000,
          finished: true,
          conclusion: '',
          tools: [],
          items: [
            { id: 'thought-terminal', kind: 'thought', icon: 'think', title: '思考', detail: '完成检查', status: 'completed' },
          ],
        },
      },
    }))

    expect(markup).toContain('aria-expanded="false"')
    expect(markup).not.toContain('thought-activity-list')
  })

  it('用户消息保留纯文本气泡，不把用户输入当成 Markdown 页面', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-user-markdown',
        role: 'user',
        content: '```python\nprint("literal")\n```',
        created_at: '2026-09-08T00:00:00.000Z',
      },
    }))

    expect(markup).not.toContain('markdown-code-block')
    expect(markup).toContain('```python')
  })

  it('即使用户消息意外带有工具名，也仍按纯文本渲染', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-user-tool-name',
        role: 'user',
        tool_name: 'Shell',
        content: '```bash\nrm example.txt\n```',
        created_at: '2026-09-08T00:00:00.000Z',
      },
    }))

    expect(markup).not.toContain('markdown-code-block')
    expect(markup).toContain('```bash')
  })

  it('不把助手输出中的原始 HTML 或 javascript 链接注入页面', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-markdown-safety',
        role: 'assistant',
        content: '<img src=x onerror="alert(1)"> [危险链接](javascript:alert(1))',
        created_at: '2026-09-08T00:00:00.000Z',
      },
    }))

    expect(markup).not.toContain('<img')
    expect(markup).not.toContain('<a onerror=')
    expect(markup).not.toContain('href="javascript:')
  })

  it('将完成的思考折叠条放在助手头像和最终输出之间', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: { id: 'message-1', role: 'assistant', content: '最终结果已完成', created_at: '2026-09-08T00:00:00.000Z' },
      thoughtRunId: 'run-1',
      thoughtTimeline: {
        startedAt: 1_000,
        elapsedMs: 14_000,
        finished: true,
        conclusion: '',
        activeItemId: undefined,
        tools: [],
        items: [{ id: 'thought-1', kind: 'thought', icon: 'think', title: '思考', detail: '先检查项目结构，再整理结果。', status: 'completed' }],
      },
    }))

    const thoughtPosition = markup.indexOf('completed-thought')
    const contentPosition = markup.indexOf('最终结果已完成')
    expect(thoughtPosition).toBeGreaterThan(markup.indexOf('message-body'))
    expect(thoughtPosition).toBeLessThan(contentPosition)
    expect(markup).toContain('执行详情 · 用时 14s')
  })

  it('没有思考文本时只显示静态耗时，不显示执行详情或上下文准备活动', () => {
    const markup = renderToStaticMarkup(createElement(CompletedThoughtTimeline, {
      runId: 'run-context-only',
      timeline: {
        startedAt: 1_000,
        elapsedMs: 9_000,
        finished: true,
        conclusion: '',
        activeItemId: undefined,
        tools: [],
        items: [{ id: 'context-1', kind: 'context', icon: 'context', title: '准备上下文', detail: '上下文准备中…', status: 'completed' }],
      },
    }))

    expect(markup).toContain('用时 9s')
    expect(markup).not.toContain('执行详情')
    expect(markup).not.toContain('准备上下文')
    expect(markup).not.toContain('上下文准备中')
  })

  it('普通步骤直接展示且只有工具调用可以展开', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-2',
        phase: '执行中',
        draft: '',
        status: 'live',
        error: '',
        thinkingStatus: '处理中',
        thought: {
          startedAt: Date.now(),
          elapsedMs: 0,
          finished: false,
          conclusion: '',
          tools: [],
          items: [
            { id: 'progress-1', kind: 'event', icon: 'think', title: '检查项目', detail: '正在读取目录结构', status: 'completed' },
            { id: 'tool-1', kind: 'tool', icon: 'shell', title: '运行命令', detail: 'npm test', status: 'running' },
          ],
        },
      },
    }))

    expect(markup).toContain('检查项目')
    expect(markup).toContain('正在读取目录结构')
    expect(markup.match(/aria-expanded="false"/g)).toHaveLength(1)
    expect(markup).toContain('npm test')
  })

  it('长网址的联网搜索可展开，短联网搜索保持紧凑', () => {
    const longUrl = '打开：https://example.com/' + 'very-long-article-slug-'.repeat(8)
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-long-url',
        phase: '执行中',
        draft: '',
        status: 'live',
        error: '',
        thinkingStatus: '处理中',
        thought: {
          startedAt: Date.now(), elapsedMs: 0, finished: false, conclusion: '', tools: [],
          items: [
            { id: 'web-long', kind: 'tool', icon: 'search', title: '联网搜索', detail: longUrl, status: 'completed' },
            { id: 'web-break', kind: 'thought', icon: 'think', title: '思考', detail: '整理搜索结果', status: 'completed' },
            { id: 'web-short', kind: 'tool', icon: 'search', title: '联网搜索', detail: '搜索：今日新闻', status: 'completed' },
          ],
        },
      },
    }))

    expect(markup.match(/aria-expanded="false"/g)).toHaveLength(1)
    expect(markup).toContain(longUrl)
    expect(markup).toContain('搜索：今日新闻')
  })
})
