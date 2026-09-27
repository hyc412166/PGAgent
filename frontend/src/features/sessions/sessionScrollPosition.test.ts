import { describe, expect, it, beforeEach } from 'vitest'
import { loadSessionScrollTop, saveSessionScrollTop } from './sessionScrollPosition'

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
    expect(loadSessionScrollTop('session-1')).toBe(420)
    expect(loadSessionScrollTop('session-2')).toBeNull()
  })

  it('忽略无效位置和损坏的存储内容', () => {
    saveSessionScrollTop('session-1', -1)
    expect(loadSessionScrollTop('session-1')).toBeNull()
    storage.set('pgagent-session-scroll-positions', '{bad json')
    expect(loadSessionScrollTop('session-1')).toBeNull()
  })
})

