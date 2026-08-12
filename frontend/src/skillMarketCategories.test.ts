import { describe, expect, it } from 'vitest'
import { fallbackMarketCategories, leaderboardRefreshDelayMs, normalizeMarketCategories } from './skillMarketCategories'

describe('Skill 市场分类榜单', () => {
  it('固定展示五个分类，并将每类限制为前六项', () => {
    const items = Array.from({ length: 8 }, (_, index) => ({
      id: `skill-${index}`,
      slug: `owner/skill-${index}`,
      name: `Skill ${index}`,
      source: 'owner',
    }))
    const categories = normalizeMarketCategories({
      categories: [{ id: 'frontend', items }],
    })

    expect(categories).toHaveLength(5)
    expect(categories.find((category) => category.id === 'frontend')?.items).toHaveLength(6)
    expect(categories.map((category) => category.id)).toEqual(['frontend', 'programming', 'research', 'writing', 'data-ai'])
  })

  it('旧市场接口的搜索结果也能转换为分类榜单', () => {
    const categories = fallbackMarketCategories({
      writing: [{ id: 'writer', slug: 'owner/writer', name: 'Writer', source: 'owner' }],
    })

    expect(categories.find((category) => category.id === 'writing')?.items?.[0]?.name).toBe('Writer')
  })

  it('自动刷新间隔保持在合理范围内', () => {
    expect(leaderboardRefreshDelayMs()).toBe(1_800_000)
    expect(leaderboardRefreshDelayMs(10)).toBe(60_000)
    expect(leaderboardRefreshDelayMs(9_999)).toBe(3_600_000)
  })
})
