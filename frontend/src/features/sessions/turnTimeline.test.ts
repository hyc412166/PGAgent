import { describe, expect, it } from 'vitest'

import { buildConversationTurnSummaries, summarizeTurnContent } from './turnTimeline'
import type { Message } from '../../types'

const message = (id: string, role: Message['role'], content: string, turn_id?: string): Message => ({ id, role, content, turn_id })

describe('会话轮次摘要', () => {
  it('按 turn_id 配对提问和回答并保留对话顺序', () => {
    const turns = buildConversationTurnSummaries([
      message('u-1', 'user', '第一问', 'turn-1'),
      message('a-1', 'assistant', '第一答', 'turn-1'),
      message('u-2', 'user', '第二问', 'turn-2'),
      message('a-2', 'assistant', '第二答', 'turn-2'),
    ])

    expect(turns.map((turn) => [turn.index, turn.id, turn.userContent, turn.assistantContent])).toEqual([
      [1, 'turn-1', '第一问', '第一答'],
      [2, 'turn-2', '第二问', '第二答'],
    ])
  })

  it('为未完成回复保留轮次并标记无回答状态', () => {
    const [turn] = buildConversationTurnSummaries([message('u-1', 'user', '等待回答', 'turn-1')])

    expect(turn).toMatchObject({ hasReply: false, userContent: '等待回答', assistantContent: '' })
  })

  it('兼容没有 turn_id 的旧消息顺序', () => {
    const turns = buildConversationTurnSummaries([
      message('u-1', 'user', '旧问题一'),
      message('a-1', 'assistant', '旧回答一'),
      message('u-2', 'user', '旧问题二'),
      message('a-2', 'assistant', '旧回答二'),
    ])

    expect(turns.map((turn) => [turn.userContent, turn.assistantContent])).toEqual([
      ['旧问题一', '旧回答一'],
      ['旧问题二', '旧回答二'],
    ])
  })

  it('折叠格式标记并截断过长预览', () => {
    expect(summarizeTurnContent('## 标题\n```ts\nconst secret = true\n```\n实际内容')).toBe('标题 实际内容')
    expect(summarizeTurnContent('abcdefghij', 6)).toBe('abcde…')
  })
})
