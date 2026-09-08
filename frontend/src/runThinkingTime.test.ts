// 本测试验证活动运行在页面重新挂载后仍沿用服务端记录的开始时间。
import { describe, expect, it } from 'vitest'
import { runThinkingStartedAt } from './features/sessions/sessionState'

describe('运行思考计时恢复', () => {
  it('优先使用运行记录的开始时间', () => {
    expect(runThinkingStartedAt('2026-09-07T12:00:00.000Z', 1_800_000_000_000)).toBe(1_788_782_400_000)
    expect(runThinkingStartedAt('2026-09-07T12:00:00.000', 1_800_000_000_000)).toBe(1_788_782_400_000)
  })

  it('开始时间缺失或无效时使用当前时间', () => {
    expect(runThinkingStartedAt(undefined, 1_800_000_000_000)).toBe(1_800_000_000_000)
    expect(runThinkingStartedAt('invalid', 1_800_000_000_000)).toBe(1_800_000_000_000)
  })
})
