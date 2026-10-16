import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as apiData from '../../shared/hooks/useApiData'
import { childReplies } from './childReplies'
import { ChildConversation } from './components/ChildConversation'

afterEach(() => vi.restoreAllMocks())

describe('子会话正文', () => {
  it('按顺序合并增量，完成正文替代增量而不是重复显示', () => {
    expect(childReplies([
      { sequence: 3, event_type: 'assistant_message_completed', payload: { item_id: 'a', content: '完整回答' } },
      { sequence: 1, event_type: 'assistant_message_delta', payload: { item_id: 'a', delta: '完整' } },
      { sequence: 2, event_type: 'assistant_message_delta', payload: { item_id: 'a', delta: '回答' } },
    ])).toEqual([{ id: ':a', content: '完整回答' }])
  })
  it('保留独立响应，不把内部快照当成回复', () => {
    expect(childReplies([
      { event_type: 'runtime_snapshot', payload: { content: '私有上下文' } },
      { sequence: 1, event_type: 'assistant_message_completed', payload: { response_id: 'r1', item_id: 'a', content: '检查文件' } },
      { sequence: 2, event_type: 'assistant_message_completed', payload: { response_id: 'r2', item_id: 'a', content: '完成修改' } },
    ]).map(item => item.content)).toEqual(['检查文件', '完成修改'])
  })
  it('协作通信中仅将子 Agent 的文件链接绑定到子运行', () => {
    vi.spyOn(apiData, 'useApiData').mockReturnValue({
      data: {
        events: [],
        messages: [
          { id: 'child-message', sender_worker_id: 'worker-1', recipient_worker_id: null, content: '[plan.md](plan.md)', created_at: '2026-10-15T10:00:00Z' },
          { id: 'lead-message', sender_worker_id: null, recipient_worker_id: 'worker-1', content: '[request.md](request.md)', created_at: '2026-10-15T09:00:00Z' },
        ],
      },
      loading: false, error: '', initialLoading: false, refreshing: false,
      reload: vi.fn(), refresh: vi.fn(), setState: vi.fn(),
    })
    const markup = renderToStaticMarkup(createElement(ChildConversation, {
      task: { id: 'task-1', title: '编写计划', child_run_id: 'child-run', teammate_id: 'worker-1', parent_session_id: 'session-1', status: 'completed' },
    }))

    expect(markup).toContain('href="/api/runs/child-run/file?path=plan.md"')
    expect(markup).toContain('href="request.md"')
  })
})
