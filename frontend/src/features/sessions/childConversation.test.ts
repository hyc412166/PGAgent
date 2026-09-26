import { describe, expect, it } from 'vitest'
import { childReplies } from './childReplies'

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
})
