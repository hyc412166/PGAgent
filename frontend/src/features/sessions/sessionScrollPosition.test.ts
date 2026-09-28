import { describe, expect, it, beforeEach } from 'vitest'
import { loadSessionScrollPosition, saveSessionScrollPosition, saveSessionScrollTop } from './sessionScrollPosition'

describe('session scroll position', () => {
  const storage = new Map<string, string>()

  beforeEach(() => {
    storage.clear()
    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: { localStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value),
      } },
    })
  })

  it('保存并按会话读取滚动位置', () => {
    saveSessionScrollTop('session-1', 420)
    expect(loadSessionScrollPosition('session-1')).toEqual({ offset: 0, scrollTop: 420 })
    saveSessionScrollPosition('session-2', { messageId: 'conversation-message-2', offset: 36, scrollTop: 820 })
    expect(loadSessionScrollPosition('session-2')).toEqual({ messageId: 'conversation-message-2', offset: 36, scrollTop: 820 })
  })

  it('忽略无效位置和损坏的存储内容', () => {
    saveSessionScrollTop('session-1', -1)
    expect(loadSessionScrollPosition('session-1')).toBeNull()
    storage.set('pgagent-session-scroll-positions', '{bad json')
    expect(loadSessionScrollPosition('session-1')).toBeNull()
  })
})
