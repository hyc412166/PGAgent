// 本测试验证技能市场只接纳最新请求，并保持分页追加与刷新替换语义。
import { describe, expect, it } from 'vitest'

import { isCurrentMarketRequest, mergeMarketBrowsePage, shouldStartLeaderboardRequest } from './marketRequestState'

describe('技能市场请求状态', () => {
  it('拒绝旧请求覆盖最新请求', () => {
    expect(isCurrentMarketRequest(4, 5)).toBe(false)
    expect(isCurrentMarketRequest(5, 5)).toBe(true)
  })

  it('加载更多追加，刷新则替换旧榜单', () => {
    const current = { items: [{ id: 'old', name: 'Old' }], page: 0 }
    const incoming = { items: [{ id: 'new', name: 'New' }], page: 1 }

    expect(mergeMarketBrowsePage(current, incoming, true).items?.map((item) => item.id)).toEqual(['old', 'new'])
    expect(mergeMarketBrowsePage(current, incoming, false).items?.map((item) => item.id)).toEqual(['new'])
  })

  it('手动刷新进行中跳过自动刷新，但手动请求仍可抢占自动请求', () => {
    expect(shouldStartLeaderboardRequest(false, true)).toBe(false)
    expect(shouldStartLeaderboardRequest(true, true)).toBe(true)
    expect(shouldStartLeaderboardRequest(false, false)).toBe(true)
  })
})
