// 本组件提供会话轮次的低干扰导航：格子用于定位，悬浮卡用于快速回忆上下文。
import { useRef, useState, type FocusEvent, type MouseEvent } from 'react'
import type { ConversationTurnSummary } from '../turnTimeline'
import { summarizeTurnContent } from '../turnTimeline'

type ConversationTurnTimelineProps = {
  turns: ConversationTurnSummary[]
}

const PREVIEW_HEIGHT = 96

export function ConversationTurnTimeline({ turns }: ConversationTurnTimelineProps) {
  const railRef = useRef<HTMLElement>(null)
  const [preview, setPreview] = useState<{ id: string; top: number } | null>(null)
  if (turns.length < 2) return null

  const showPreview = (event: MouseEvent<HTMLButtonElement> | FocusEvent<HTMLButtonElement>, turn: ConversationTurnSummary) => {
    const rail = railRef.current
    const button = event.currentTarget
    if (!rail) return
    const center = button.offsetTop + button.offsetHeight / 2
    const maxTop = Math.max(8, rail.clientHeight - PREVIEW_HEIGHT - 8)
    setPreview({ id: turn.id, top: Math.max(8, Math.min(maxTop, center - PREVIEW_HEIGHT / 2)) })
  }

  const hidePreview = () => setPreview(null)
  const scrollToTurn = (turn: ConversationTurnSummary) => {
    const target = document.getElementById(`conversation-message-${turn.anchorMessageId}`)
    const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
    target?.scrollIntoView({ behavior, block: 'center' })
  }

  const selectedTurn = preview ? turns.find((turn) => turn.id === preview.id) : undefined
  const previewIndex = preview ? turns.findIndex((turn) => turn.id === preview.id) : -1
  return <nav ref={railRef} className="conversation-turn-timeline" aria-label="会话轮次导航">
    <div className="conversation-turn-timeline-list">
      {turns.map((turn, index) => {
        const prompt = summarizeTurnContent(turn.userContent) || '未记录提问'
        const reply = summarizeTurnContent(turn.assistantContent)
        const label = `第 ${turn.index} 轮：提问 ${prompt}；${reply ? `回答 ${reply}` : '回答尚未生成'}`
        const distance = previewIndex === -1 ? 0 : Math.abs(index - previewIndex)
        const proximityClass = distance === 1 ? 'is-near-1' : distance === 2 ? 'is-near-2' : ''
        return <button
          key={turn.id}
          type="button"
          className={`conversation-turn-marker ${!turn.hasReply ? 'is-pending' : ''} ${preview?.id === turn.id ? 'is-previewing' : ''} ${proximityClass}`}
          aria-label={label}
          aria-describedby={preview?.id === turn.id ? `conversation-turn-preview-${turn.id}` : undefined}
          onMouseEnter={(event) => showPreview(event, turn)}
          onMouseLeave={hidePreview}
          onFocus={(event) => showPreview(event, turn)}
          onBlur={hidePreview}
          onClick={() => scrollToTurn(turn)}
        ><span aria-hidden="true" /></button>
      })}
    </div>
    {selectedTurn && <div
      id={`conversation-turn-preview-${selectedTurn.id}`}
      className="conversation-turn-preview"
      role="tooltip"
      style={{ top: preview?.top }}
    >
      <p className="conversation-turn-preview-prompt">{summarizeTurnContent(selectedTurn.userContent) || '未记录提问'}</p>
      <p className="conversation-turn-preview-reply">{selectedTurn.hasReply ? summarizeTurnContent(selectedTurn.assistantContent) || '回答为空' : '正在生成回答…'}</p>
    </div>}
  </nav>
}
