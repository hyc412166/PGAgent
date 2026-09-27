const SESSION_SCROLL_STORAGE_KEY = 'pgagent-session-scroll-positions'

type ScrollPositions = Record<string, number>

function readPositions(): ScrollPositions {
  try {
    const raw = window.localStorage.getItem(SESSION_SCROLL_STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return Object.fromEntries(Object.entries(parsed).filter(([, value]) => typeof value === 'number' && Number.isFinite(value) && value >= 0))
  } catch {
    return {}
  }
}

export function loadSessionScrollTop(sessionId: string): number | null {
  if (!sessionId) return null
  const value = readPositions()[sessionId]
  return typeof value === 'number' ? value : null
}

export function saveSessionScrollTop(sessionId: string, scrollTop: number): void {
  if (!sessionId || !Number.isFinite(scrollTop) || scrollTop < 0) return
  try {
    const positions = readPositions()
    positions[sessionId] = scrollTop
    window.localStorage.setItem(SESSION_SCROLL_STORAGE_KEY, JSON.stringify(positions))
  } catch {
    // 本地存储不可用时仍保持当前页面滚动状态，不影响会话操作。
  }
}

