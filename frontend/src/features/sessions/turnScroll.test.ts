import { describe, expect, it } from 'vitest'
import { turnScrollDuration } from './turnScroll'

describe('轮次定位动画范围', () => {
  it('四个视口以内双向滚动固定为600ms，超出立即跳转', () => {
    expect(turnScrollDuration(2100, 700, false)).toBe(600)
    expect(turnScrollDuration(-2800, 700, false)).toBe(600)
    expect(turnScrollDuration(2801, 700, false)).toBe(0)
    expect(turnScrollDuration(-3500, 700, false)).toBe(0)
  })
  it('尊重减少动态效果设置', () => {
    expect(turnScrollDuration(200, 700, true)).toBe(0)
  })
})
