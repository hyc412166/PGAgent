import type { SkillMarketplaceCategory, SkillMarketplaceItem, SkillMarketplaceLeaderboards } from './types'

export type MarketCategoryDefinition = {
  id: string
  label: string
  description: string
  query: string
}

export const marketCategoryDefinitions: MarketCategoryDefinition[] = [
  { id: 'frontend', label: '前端开发', description: '界面、交互、设计系统与 Web 开发', query: 'frontend web ui' },
  { id: 'programming', label: '编程开发', description: '工程实践、代码质量与开发效率', query: 'programming software development' },
  { id: 'research', label: '论文研究', description: '文献检索、学术研究与论文工作流', query: 'research paper academic' },
  { id: 'writing', label: '写作内容', description: '内容创作、编辑、文案与表达', query: 'writing content editing' },
  { id: 'data-ai', label: '数据分析 / AI', description: '数据处理、建模、机器学习与 AI', query: 'data analysis ai machine learning' },
]

function cleanText(value?: string) {
  return value?.trim() || undefined
}

function categoryMatches(definition: MarketCategoryDefinition, category: SkillMarketplaceCategory) {
  const aliases = new Set([definition.id, definition.label])
  return [category.id, category.label, category.name, category.title, category.query]
    .filter((value): value is string => !!cleanText(value))
    .some((value) => aliases.has(value.trim()))
}

export function normalizeMarketCategories(payload: SkillMarketplaceLeaderboards): SkillMarketplaceCategory[] {
  const incoming = Array.isArray(payload.categories) ? payload.categories : []
  return marketCategoryDefinitions.map((definition) => {
    const matched = incoming.find((category) => categoryMatches(definition, category))
    return {
      ...definition,
      ...matched,
      id: definition.id,
      label: cleanText(matched?.label) || cleanText(matched?.name) || cleanText(matched?.title) || definition.label,
      description: cleanText(matched?.description) || definition.description,
      query: cleanText(matched?.query) || definition.query,
      items: (matched?.items ?? []).slice(0, 6),
    }
  })
}

export function fallbackMarketCategories(itemsByCategory: Record<string, SkillMarketplaceItem[]>): SkillMarketplaceCategory[] {
  return marketCategoryDefinitions.map((definition) => ({
    ...definition,
    items: (itemsByCategory[definition.id] ?? []).slice(0, 6),
  }))
}

export function leaderboardRefreshDelayMs(seconds?: number) {
  const defaultSeconds = 30 * 60
  const normalized = Number.isFinite(seconds) ? Math.floor(seconds as number) : defaultSeconds
  return Math.min(60 * 60, Math.max(60, normalized || defaultSeconds)) * 1000
}
