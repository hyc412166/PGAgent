// 本测试文件验证会话侧栏中的持久运行事件只使用安全展示模型。
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { ChildAgentPanel, LiveAssistantMessage, MessageBubble } from './presentation'

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
