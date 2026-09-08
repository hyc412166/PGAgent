// 本测试文件验证 skillMarketCategories 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'
import { fallbackMarketCategories, leaderboardRefreshDelayMs, normalizeMarketCategories } from './skillMarketCategories'

// 测试分组：Skill 市场分类榜单。
describe('Skill 市场分类榜单', () => {
  // 测试场景：固定展示五个分类，并将每类限制为前六项。
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

  // 测试场景：旧市场接口的搜索结果也能转换为分类榜单。
  it('旧市场接口的搜索结果也能转换为分类榜单', () => {
    const categories = fallbackMarketCategories({
      writing: [{ id: 'writer', slug: 'owner/writer', name: 'Writer', source: 'owner' }],
    })

    expect(categories.find((category) => category.id === 'writing')?.items?.[0]?.name).toBe('Writer')
  })

  // 测试场景：自动刷新间隔保持在合理范围内。
  it('自动刷新间隔保持在合理范围内', () => {
    expect(leaderboardRefreshDelayMs()).toBe(1_800_000)
    expect(leaderboardRefreshDelayMs(10)).toBe(60_000)
    expect(leaderboardRefreshDelayMs(9_999)).toBe(3_600_000)
  })
})
