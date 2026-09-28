const SESSION_SCROLL_STORAGE_KEY = 'pgagent-session-scroll-positions'

export type SessionScrollPosition = {
  messageId?: string
  offset: number
  scrollTop: number
}

type ScrollPositions = Record<string, SessionScrollPosition>

function readPositions(): ScrollPositions {
  try {
    const raw = window.localStorage.getItem(SESSION_SCROLL_STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return Object.fromEntries(Object.entries(parsed).flatMap(([sessionId, value]) => {
      if (typeof value === 'number' && Number.isFinite(value) && value >= 0) {
        return [[sessionId, { offset: 0, scrollTop: value }]]
      }
      if (!value || typeof value !== 'object' || Array.isArray(value)) return []
      const record = value as Record<string, unknown>
      const offset = typeof record.offset === 'number' && Number.isFinite(record.offset) ? record.offset : 0
      const scrollTop = record.scrollTop
      if (typeof scrollTop !== 'number' || !Number.isFinite(scrollTop) || scrollTop < 0) return []
      return [[sessionId, {
        messageId: typeof record.messageId === 'string' && record.messageId ? record.messageId : undefined,
        offset,
        scrollTop,
      }]]
    }))
  } catch {
    return {}
  }
}

export function loadSessionScrollPosition(sessionId: string): SessionScrollPosition | null {
  if (!sessionId) return null
  const value = readPositions()[sessionId]
  return value ?? null
}

export function saveSessionScrollPosition(sessionId: string, position: SessionScrollPosition): void {
  if (!sessionId || !Number.isFinite(position.scrollTop) || position.scrollTop < 0) return
  try {
    const positions = readPositions()
    positions[sessionId] = position
    window.localStorage.setItem(SESSION_SCROLL_STORAGE_KEY, JSON.stringify(positions))
  } catch {
    // 本地存储不可用时仍保持当前页面滚动状态，不影响会话操作。
  }
}

export function saveSessionScrollTop(sessionId: string, scrollTop: number): void {
  if (!sessionId || !Number.isFinite(scrollTop) || scrollTop < 0) return
  saveSessionScrollPosition(sessionId, { offset: 0, scrollTop })
}
