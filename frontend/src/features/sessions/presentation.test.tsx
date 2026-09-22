// 本测试文件验证会话侧栏中的持久运行事件只使用安全展示模型。
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'

import { ChildAgentPanel, CompletedThoughtTimeline, FileChangeActivity, LiveAssistantMessage, MessageBubble } from './presentation'
import { createLiveMarkdownCoalescer } from './liveMarkdownCoalescer'
import { formatApprovalArguments } from './approvalPresentation'
import { groupThoughtActivities } from './thoughtActivityGrouping'

const sessionsCss = readFileSync(new URL('../../styles/sessions.css', import.meta.url), 'utf8')

describe('子 Agent 运行事件展示', () => {
  it('将审批参数格式化为可读内容并脱敏敏感字段', () => {
    expect(formatApprovalArguments('shell', { command: 'Get-ChildItem -Force', cwd: 'C:\\repo', timeout_seconds: 30 }))
      .toContain('Get-ChildItem -Force')
    expect(formatApprovalArguments('http', { url: 'https://example.com', api_token: 'secret-value' }))
      .toContain('[已隐藏]')
    expect(formatApprovalArguments('http', {})).toBe('无参数')
  })

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
  it('合并高频流式正文更新，并在结束时立即刷新最终内容', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-15T00:00:00.000Z'))
    const commits: string[] = []
    const coalescer = createLiveMarkdownCoalescer('初始', (content) => commits.push(content))

    try {
      coalescer.push('第一段')
      vi.advanceTimersByTime(40)
      coalescer.push('第二段')
      vi.advanceTimersByTime(59)
      expect(commits).toEqual([])

      vi.advanceTimersByTime(1)
      expect(commits).toEqual(['第二段'])

      coalescer.push('尚未提交')
      coalescer.flush('完整最终回复')
      expect(commits).toEqual(['第二段', '完整最终回复'])
      vi.advanceTimersByTime(100)
      expect(commits).toEqual(['第二段', '完整最终回复'])
    } finally {
      coalescer.dispose()
      vi.useRealTimers()
    }
  })

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

  it('连续联网搜索默认使用放大镜图标而不是终端图标', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-web-search-group',
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
            { id: 'search-1', kind: 'tool', icon: 'search', title: '联网搜索', detail: '搜索：latest US news', status: 'completed' },
            { id: 'search-2', kind: 'tool', icon: 'search', title: '联网搜索', detail: '搜索：latest China news', status: 'completed' },
          ],
        },
      },
    }))

    expect(markup).toContain('is-search')
    expect(markup).not.toContain('is-shell')
  })

  it('工具分组标题使用与文字同高的语义图标容器', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-aligned-search-group',
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
            { id: 'aligned-search-1', kind: 'tool', icon: 'search', title: '联网搜索', detail: '搜索：latest US news', status: 'completed' },
            { id: 'aligned-search-2', kind: 'tool', icon: 'search', title: '联网搜索', detail: '搜索：latest China news', status: 'completed' },
          ],
        },
      },
    }))

    expect(markup).toContain('tool-activity-icon thought-activity-icon is-search')
  })

  it('工具活动图标不会继承起始对齐并在展开详情时保持标题行对齐', () => {
    expect(sessionsCss).toMatch(/\.tool-activity-icon\s*\{[^}]*align-self:\s*center;/s)
    expect(sessionsCss).toContain('.thought-activity.kind-tool { align-items: start; }')
    expect(sessionsCss).toContain('.thought-activity.kind-tool .thought-activity-icon { align-self: start; transform: translateY(1px); }')
    expect(sessionsCss).toContain('.tool-activity-group-item-line > .tool-activity-icon { flex: 0 0 20px; transform: translateY(1px); }')
    expect(sessionsCss).toContain('.tool-activity-group-item-line > span:not(.tool-activity-icon) {')
  })

  it('把等待下一条可见输出的状态放进执行详情，并复用工具行动效规范', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2_000))
    try {
      const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
        liveRun: {
          runId: 'run-thinking-wait',
          phase: '思考中…',
          draft: '',
          status: 'live',
          error: '',
          thinkingStatus: '正在整理思路中… (•̀ᴗ•́)و',
          thought: {
            startedAt: 1_000,
            activeStepStartedAt: 1_000,
            activeStepHasVisibleContent: false,
            elapsedMs: 0,
            finished: false,
            conclusion: '',
            tools: [],
            items: [],
          },
        },
      }))

      expect(markup).toContain('class="thought-waiting"')
      expect(markup).toContain('正在整理思路中… (•̀ᴗ•́)و')
      expect(markup).toContain('1 秒')
      expect(markup).not.toMatch(/<span class="live-phase">[^<]*正在整理思路/)
      expect(sessionsCss).toContain('@keyframes thought-wait-character')
      expect(sessionsCss).toContain('@keyframes thought-wait-breathe')
      expect(sessionsCss).toContain('@keyframes thought-wait-shimmer')
      expect(sessionsCss).toContain('.thought-waiting::after')

      const withVisibleSummary = renderToStaticMarkup(createElement(LiveAssistantMessage, {
        liveRun: {
          runId: 'run-thinking-visible',
          phase: '执行中',
          draft: '',
          status: 'live',
          error: '',
          thinkingStatus: '正在整理思路中… (•̀ᴗ•́)و',
          thought: {
            startedAt: 1_000,
            activeStepStartedAt: 1_000,
            activeStepHasVisibleContent: true,
            elapsedMs: 0,
            finished: false,
            conclusion: '',
            tools: [],
            items: [{ id: 'summary-1', kind: 'thought', icon: 'think', title: '思考', detail: '已完成扫描。', status: 'running' }],
          },
        },
      }))
      expect(withVisibleSummary).not.toContain('class="thought-waiting"')
    } finally {
      vi.useRealTimers()
    }
  })

  it('旧工具详情存在但当前轮元数据缺失时仍显示等待行，并将等待行置于详情顶部', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-missing-step-boundary',
        phase: '正在回复…',
        draft: '',
        status: 'live',
        error: '',
        thinkingStatus: '',
        thought: {
          startedAt: 1_000,
          elapsedMs: 0,
          finished: false,
          conclusion: '',
          tools: [{ id: 'tool-1', name: 'Shell', target: 'Get-ChildItem', status: 'completed' }],
          items: [{ id: 'tool-1', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'Get-ChildItem', status: 'completed' }],
        },
      },
    }))

    expect(markup).toContain('class="thought-waiting"')
    expect(markup).toContain('思考中…')
    expect(markup.indexOf('class="thought-waiting"')).toBeLessThan(markup.indexOf('Get-ChildItem'))
  })

  it('文件总结超过三个时默认收起并提供展开入口', () => {
    const markup = renderToStaticMarkup(createElement(FileChangeActivity, {
      runId: 'run-file-summary',
      title: '已编辑 4 个文件',
      status: 'completed',
      changeSet: {
        file_count: 4,
        files: [
          { path: 'one.py', operation: 'update', added_lines: 1, deleted_lines: 0 },
          { path: 'two.py', operation: 'update', added_lines: 2, deleted_lines: 1 },
          { path: 'three.py', operation: 'update', added_lines: 3, deleted_lines: 2 },
          { path: 'four.py', operation: 'update', added_lines: 4, deleted_lines: 3 },
        ],
      },
      onOpenFileChange: vi.fn(),
    }))

    expect(markup).toContain('one.py')
    expect(markup).toContain('three.py')
    expect(markup).not.toContain('four.py')
    expect(markup).toContain('再显示 1 个文件')
  })

  it('执行中的文件编辑行使用普通灰色工具样式且不显示详情箭头', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-inline-file', phase: '执行中', draft: '', status: 'live', error: '', thinkingStatus: '处理中',
        thought: {
          startedAt: 1_000, elapsedMs: 0, finished: false, conclusion: '', tools: [],
          items: [{
            id: 'edit-config', kind: 'tool', icon: 'edit', title: '已编辑 config.py', detail: '+12 −3', status: 'completed',
            changeSet: { file_count: 1, files: [{ path: 'config.py', operation: 'update', added_lines: 12, deleted_lines: 3 }] },
          }],
        },
      },
    }))

    expect(markup).toContain('已编辑 config.py')
    expect(markup).toContain('+12 −3')
    expect(markup).not.toContain('thought-activity-chevron')
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

  it('将反斜杠分隔的块级 LaTeX 渲染为公式而不是代码面板', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-latex-display',
        role: 'assistant',
        content: '论文用 Headroom-Closed Index：\n\n\\[\nH=100\\times\\frac{s-F_{0}}{100-F_{0}}\n\\]',
        created_at: '2026-09-15T00:00:00.000Z',
      },
    }))

    expect(markup).toContain('katex-display')
    expect(markup).toContain('<math')
    expect(markup).not.toContain('markdown-code-block')
  })

  it('将反斜杠分隔的行内 LaTeX 保留在正文行内', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-latex-inline',
        role: 'assistant',
        content: '其中 \\(P\\) 表示跨轮次持久性。',
        created_at: '2026-09-15T00:00:00.000Z',
      },
    }))

    expect(markup).toContain('katex-mathml')
    expect(markup).toContain('<mi>P</mi>')
    expect(markup).not.toContain('markdown-math-block')
  })

  it('实时助手回复复用块级 LaTeX 渲染', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-latex-draft',
        phase: '生成回复',
        draft: '\\[\nR=(P,S,E,D,M,V,G)\n\\]',
        status: 'live',
        error: '',
        thinkingStatus: '处理中',
        thought: { startedAt: null, elapsedMs: 0, finished: false, conclusion: '', tools: [], items: [] },
      },
    }))

    expect(markup).toContain('katex-display')
    expect(markup).not.toContain('markdown-code-block')
  })

  it('代码范围内的 LaTeX 分隔符保持原样', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-latex-code',
        role: 'assistant',
        content: '`\\(P\\)`\n\n```text\n\\[\nH=100\\times\\frac{s-F_0}{100-F_0}\n\\]\n```',
        created_at: '2026-09-15T00:00:00.000Z',
      },
    }))

    expect(markup).not.toContain('class="katex')
    expect(markup).toContain('markdown-code-block')
    expect(markup).toContain('\\(P\\)')
    expect(markup).toContain('\\frac')
  })

  it('math fenced code 仍作为可复制的 LaTeX 源码展示', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-latex-math-fence',
        role: 'assistant',
        content: '```math\nH=100\\times\\frac{s-F_0}{100-F_0}\n```',
        created_at: '2026-09-15T00:00:00.000Z',
      },
    }))

    expect(markup).not.toContain('class="katex')
    expect(markup).toContain('markdown-code-block')
    expect(markup).toContain('LaTeX')
    expect(markup).toContain('\\frac')
  })

  it('跨行 inline code 中的 LaTeX 分隔符保持原样', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-latex-multiline-code-span',
        role: 'assistant',
        content: '``示例\n\\(P\\)\n结束``',
        created_at: '2026-09-15T00:00:00.000Z',
      },
    }))

    expect(markup).not.toContain('class="katex')
    expect(markup).toContain('\\(P\\)')
    expect(markup).not.toContain('$$P$$')
  })

  it('未闭合的块级分隔符不会阻断后续合法行内公式', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-latex-unclosed-display',
        role: 'assistant',
        content: '\\[\n这段分隔符尚未闭合。\n\n后续 \\(P\\) 仍应显示为公式。',
        created_at: '2026-09-15T00:00:00.000Z',
      },
    }))

    expect(markup).toContain('class="katex')
    expect(markup).toContain('<mi>P</mi>')
    expect(markup).toContain('[')
  })

  it('引用中的 fenced code 不改写 LaTeX 分隔符', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-latex-blockquote-fence',
        role: 'assistant',
        content: '> ~~~text\n> \\(P\\)\n> ~~~',
        created_at: '2026-09-15T00:00:00.000Z',
      },
    }))

    expect(markup).not.toContain('class="katex')
    expect(markup).toContain('\\(P\\)')
    expect(markup).not.toContain('$$P$$')
  })

  it('缩进代码不改写 LaTeX 分隔符', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-latex-indented-code',
        role: 'assistant',
        content: '    \\(P\\)\n    \\[\n    H=100\\times\\frac{s-F_0}{100-F_0}\n    \\]',
        created_at: '2026-09-15T00:00:00.000Z',
      },
    }))

    expect(markup).not.toContain('class="katex')
    expect(markup).toContain('\\(P\\)')
    expect(markup).toContain('\\frac')
  })

  it('链接目标中的反斜杠括号不被当作行内公式', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: {
        id: 'message-latex-link-destination',
        role: 'assistant',
        content: '[文档](https://example.test/a\\(b\\))',
        created_at: '2026-09-15T00:00:00.000Z',
      },
    }))

    expect(markup).not.toContain('class="katex')
    expect(markup).toContain('href="https://example.test/a(b)"')
    expect(markup).not.toContain('$$')
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

  it('收起实时执行详情时隐藏中间回复但保留最终回复', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-collapsed-commentary',
        phase: '已完成',
        draft: '',
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
            { id: 'commentary-collapsed', kind: 'assistant', icon: 'think', title: '中间回复', detail: '这是中间过程。', phase: 'commentary', status: 'completed' },
            { id: 'final-collapsed', kind: 'assistant', icon: 'think', title: '最终回复', detail: '这是最终结果。', phase: 'final_answer', status: 'completed' },
          ],
        },
      },
    }))

    expect(markup).not.toContain('这是中间过程。')
    expect(markup).toContain('这是最终结果。')
  })

  it('最终回复位于执行详情容器之外，收起详情不会隐藏最终回复', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-final-outside-details',
        phase: '已完成',
        draft: '',
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
            { id: 'commentary-outside', kind: 'assistant', icon: 'think', title: '中间回复', detail: '执行中的说明。', phase: 'commentary', status: 'completed' },
            { id: 'final-outside', kind: 'assistant', icon: 'think', title: '最终回复', detail: '独立的最终回复。', phase: 'final_answer', status: 'completed' },
          ],
        },
      },
    }))

    const detailsStart = markup.indexOf('<div class="live-thought')
    const finalStart = markup.indexOf('<div class="live-final-content')
    expect(detailsStart).toBeGreaterThanOrEqual(0)
    expect(finalStart).toBeGreaterThan(detailsStart)
    expect(markup.slice(detailsStart, finalStart)).not.toContain('独立的最终回复。')
    expect(markup.slice(finalStart)).toContain('独立的最终回复。')
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

  it('只有上下文准备时只显示静态耗时，不显示执行详情', () => {
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

  it('没有思考文本但有工具调用时仍显示执行详情', () => {
    const markup = renderToStaticMarkup(createElement(CompletedThoughtTimeline, {
      runId: 'run-tool-only',
      timeline: {
        startedAt: 1_000,
        elapsedMs: 9_000,
        finished: true,
        conclusion: '',
        activeItemId: undefined,
        tools: [{ id: 'tool-1', name: 'Shell', target: 'Get-Content probe.txt', status: 'completed' }],
        items: [{ id: 'tool-1', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'Get-Content probe.txt', status: 'completed' }],
      },
    }))

    expect(markup).toContain('执行详情 · 用时 9s')
    expect(markup).not.toContain('completed-thought-static')
  })

  it('实时输出按中间回复、工具调用、后续回复和最终回复的顺序渲染', () => {
    const markup = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: {
        runId: 'run-ordered-output',
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
          activeItemId: undefined,
          tools: [{ id: 'tool-ordered', name: 'Shell', target: 'rg -n result', status: 'completed' }],
          items: [
            { id: 'commentary-1', kind: 'assistant', icon: 'think', title: '中间回复', detail: '先检查项目。', phase: 'commentary', status: 'completed' },
            { id: 'tool-ordered', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'rg -n result', status: 'completed' },
            { id: 'commentary-2', kind: 'assistant', icon: 'think', title: '中间回复', detail: '已找到结果。', phase: 'commentary', status: 'completed' },
            { id: 'final-1', kind: 'assistant', icon: 'think', title: '最终回复', detail: '最终答案。', phase: 'final_answer', status: 'completed' },
          ],
        },
      },
    }))

    expect(markup.indexOf('先检查项目。')).toBeLessThan(markup.indexOf('rg -n result'))
    expect(markup.indexOf('rg -n result')).toBeLessThan(markup.indexOf('已找到结果。'))
    expect(markup.indexOf('已找到结果。')).toBeLessThan(markup.indexOf('最终答案。'))
  })

  it('终态水合后仍保留 commentary，且不会重复已经落盘的最终回复', () => {
    const markup = renderToStaticMarkup(createElement(MessageBubble, {
      message: { id: 'message-terminal-commentary', role: 'assistant', content: '最终答案。', created_at: '2026-09-08T00:00:00.000Z' },
      thoughtRunId: 'run-terminal-commentary',
      thoughtTimeline: {
        startedAt: 1_000,
        elapsedMs: 4_000,
        finished: true,
        conclusion: '',
        activeItemId: undefined,
        tools: [{ id: 'tool-terminal', name: 'Shell', target: 'rg -n result', status: 'completed' }],
        items: [
          { id: 'commentary-terminal', kind: 'assistant', icon: 'think', title: '中间回复', detail: '终态前的说明。', phase: 'commentary', status: 'completed' },
          { id: 'tool-terminal', kind: 'tool', icon: 'shell', title: 'Shell', detail: 'rg -n result', status: 'completed' },
          { id: 'final-terminal', kind: 'assistant', icon: 'think', title: '最终回复', detail: '最终答案。', phase: 'final_answer', status: 'completed' },
        ],
      },
    }))

    expect(markup.indexOf('终态前的说明。')).toBeLessThan(markup.indexOf('最终答案。'))
    expect(markup.match(/最终答案。/g)).toHaveLength(1)
    expect(markup).toContain('执行详情 · 用时 4s')
  })

  it('普通步骤直接展示，Shell 工具保留单条详情展开箭头', () => {
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
