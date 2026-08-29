import { describe, expect, it } from 'vitest'

import { formatRunEventTime, presentRunEvent, runDisplayTitle, runSecondaryLabel } from './runEventPresentation'
import type { Run, RunEvent } from './types'

describe('run record presentation', () => {
  it('uses the conversation title and keeps the run id as secondary metadata', () => {
    const run = {
      id: '01bcbd44-aebe-4d41-9da3-2a1782c5a3de',
      session_title: '调用 Playwright 搜索刘亦菲',
      run_kind: 'initial',
      agent_name: 'PGAgent 主控',
    } satisfies Run

    expect(runDisplayTitle(run)).toBe('调用 Playwright 搜索刘亦菲')
    expect(runSecondaryLabel(run)).toBe('初始运行 · PGAgent 主控 · 01bcbd44')
  })

  it('describes a tool call with its tool name and safe argument summaries', () => {
    const event = {
      event_type: 'tool_started',
      step: 2,
      payload: {
        tool_name: 'mcp__playwright__browser_navigate',
        arguments: {
          url: 'https://www.google.com/',
          element: { chars: 6 },
          submit: true,
          query: { chars: 3 },
          command: { executable: 'python', argument_count: 2 },
        },
        elapsed_ms: 1420,
      },
    } satisfies RunEvent

    const presented = presentRunEvent(event)

    expect(presented.title).toBe('开始调用 mcp__playwright__browser_navigate')
    expect(presented.detail).toBe('工具正在执行')
    expect(presented.facts).toEqual(expect.arrayContaining([
      { label: '网址', value: 'https://www.google.com/' },
      { label: '元素描述', value: '6 个字符' },
      { label: '提交', value: '是' },
      { label: '查询内容', value: '3 个字符' },
      { label: '命令', value: 'python · 2 个参数' },
      { label: '运行到', value: '1.4 秒' },
    ]))
  })

  it('translates common payload labels and controlled status values', () => {
    const presented = presentRunEvent({
      event_type: 'terminal_response_persisted',
      payload: { phase: 'model_output', ok: true, status: 'completed' },
    })

    expect(presented.facts).toEqual([
      { label: '阶段', value: '模型输出' },
      { label: '调用成功', value: '是' },
      { label: '状态', value: '已完成' },
    ])
  })

  it('shows the initiating message and context statistics for context preparation', () => {
    const event = {
      event_type: 'context_prepared',
      payload: {
        message_excerpt: '调用 Playwright MCP 打开浏览器并搜索科比…',
        estimated_tokens: 1342,
        omitted_messages: 2,
      },
    } satisfies RunEvent

    const presented = presentRunEvent(event)

    expect(presented.title).toBe('上下文已准备')
    expect(presented.detail).toBe('用户消息：调用 Playwright MCP 打开浏览器并搜索科比…')
    expect(presented.facts).toEqual([
      { label: '上下文', value: '1,342 Token' },
      { label: '省略历史消息', value: '2 条' },
    ])
  })

  it('formats event timestamps down to seconds', () => {
    expect(formatRunEventTime('2026-08-29T02:58:46')).toMatch(/02:58:46/)
  })
})
