// 本文件把持久消息压缩为可供会话轮次轨道使用的轻量摘要，不改变后端消息契约。
import type { Message } from '../../types'

export type ConversationTurnSummary = {
  id: string
  index: number
  anchorMessageId: string
  userContent: string
  assistantContent: string
  hasReply: boolean
  userMessage?: Message
  assistantMessage?: Message
}

type TurnDraft = {
  id: string
  firstIndex: number
  anchorMessageId: string
  userMessage?: Message
  assistantMessage?: Message
}

function messageTurnId(message: Message): string {
  const direct = typeof message.turn_id === 'string' ? message.turn_id.trim() : ''
  if (direct) return direct
  const metadataId = message.metadata?.turn_id
  return typeof metadataId === 'string' ? metadataId.trim() : ''
}

function newLegacyTurn(message: Message, index: number): TurnDraft {
  return {
    id: `legacy:${message.id}`,
    firstIndex: index,
    anchorMessageId: message.id,
  }
}

// 新消息使用 turn_id 精确配对；旧数据缺少该字段时，只按用户消息开启新组并把后续助手消息归入当前组。
export function buildConversationTurnSummaries(messages: Message[]): ConversationTurnSummary[] {
  const drafts = new Map<string, TurnDraft>()
  let legacyCurrent: TurnDraft | undefined

  messages.forEach((message, index) => {
    if (message.role !== 'user' && message.role !== 'assistant') return
    const explicitId = messageTurnId(message)
    let draft: TurnDraft | undefined

    if (explicitId) {
      draft = drafts.get(explicitId)
      if (!draft) {
        draft = { id: explicitId, firstIndex: index, anchorMessageId: message.id }
        drafts.set(explicitId, draft)
      }
    } else if (message.role === 'user') {
      if (!legacyCurrent || legacyCurrent.userMessage) legacyCurrent = newLegacyTurn(message, index)
      draft = legacyCurrent
      drafts.set(draft.id, draft)
    } else {
      draft = legacyCurrent && !legacyCurrent.assistantMessage
        ? legacyCurrent
        : newLegacyTurn(message, index)
      drafts.set(draft.id, draft)
    }

    if (message.role === 'user' && !draft.userMessage) {
      draft.userMessage = message
      draft.anchorMessageId = message.id
    } else if (message.role === 'assistant') {
      draft.assistantMessage = message
    }
  })

  return [...drafts.values()]
    .sort((left, right) => left.firstIndex - right.firstIndex)
    .map((draft, index) => ({
      id: draft.id,
      index: index + 1,
      anchorMessageId: draft.anchorMessageId,
      userContent: draft.userMessage?.content || '',
      assistantContent: draft.assistantMessage?.content || '',
      hasReply: Boolean(draft.assistantMessage),
      userMessage: draft.userMessage,
      assistantMessage: draft.assistantMessage,
    }))
}

// 预览卡只展示短摘要；折叠 Markdown 标记可以避免代码围栏或标题破坏卡片密度。
export function summarizeTurnContent(content: string, maxLength = 96): string {
  const normalized = content
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[`*_>#~]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!normalized) return ''
  if (normalized.length <= maxLength) return normalized
  return `${normalized.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`
}
