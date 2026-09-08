// 本文件负责 skillMarketCategories 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { SkillMarketplaceCategory, SkillMarketplaceItem, SkillMarketplaceLeaderboards } from './types'

// MarketCategoryDefinition 定义本地稳定分类 ID、展示名称及匹配关键词。
export type MarketCategoryDefinition = {
  id: string
  label: string
  description: string
  query: string
}

// 分类顺序同时决定市场页面的榜单展示顺序。
export const marketCategoryDefinitions: MarketCategoryDefinition[] = [
  { id: 'frontend', label: '前端开发', description: '界面、交互、设计系统与 Web 开发', query: 'frontend web ui' },
  { id: 'programming', label: '编程开发', description: '工程实践、代码质量与开发效率', query: 'programming software development' },
  { id: 'research', label: '论文研究', description: '文献检索、学术研究与论文工作流', query: 'research paper academic' },
  { id: 'writing', label: '写作内容', description: '内容创作、编辑、文案与表达', query: 'writing content editing' },
  { id: 'data-ai', label: '数据分析 / AI', description: '数据处理、建模、机器学习与 AI', query: 'data analysis ai machine learning' },
]

// 统一大小写和首尾空白，供名称及 slug 的宽松匹配。
function cleanText(value?: string) {
  return value?.trim() || undefined
}

// 同时按分类 ID 和关键词识别后端分类，兼容市场元数据变体。
function categoryMatches(definition: MarketCategoryDefinition, category: SkillMarketplaceCategory) {
  const aliases = new Set([definition.id, definition.label])
  return [category.id, category.label, category.name, category.title, category.query]
    .filter((value): value is string => !!cleanText(value))
    .some((value) => aliases.has(value.trim()))
}

// 将后端分类归入本地定义并去重，输出稳定顺序。
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

// 后端无分类描述时，从按分类分组的条目构造可展示榜单。
export function fallbackMarketCategories(itemsByCategory: Record<string, SkillMarketplaceItem[]>): SkillMarketplaceCategory[] {
  return marketCategoryDefinitions.map((definition) => ({
    ...definition,
    items: (itemsByCategory[definition.id] ?? []).slice(0, 6),
  }))
}

// 将后端建议秒数转换为受控毫秒间隔，供后台刷新定时器使用。
export function leaderboardRefreshDelayMs(seconds?: number) {
  const defaultSeconds = 30 * 60
  const normalized = Number.isFinite(seconds) ? Math.floor(seconds as number) : defaultSeconds
  return Math.min(60 * 60, Math.max(60, normalized || defaultSeconds)) * 1000
}
