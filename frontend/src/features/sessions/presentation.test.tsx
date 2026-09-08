// 本测试文件验证会话侧栏中的持久运行事件只使用安全展示模型。
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { ChildAgentPanel } from './presentation'

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
