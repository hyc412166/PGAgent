import type { SkillMarketplaceBrowse } from '../../types'

// 只有最新请求可以写入状态，避免较慢的旧响应覆盖用户刚切换的视图。
export function isCurrentMarketRequest(requestId: number, latestRequestId: number) {
  return requestId === latestRequestId
}

export function shouldStartLeaderboardRequest(manual: boolean, requestInFlight: boolean) {
  return manual || !requestInFlight
}

// 加载更多追加条目；切换视图或刷新时使用新页替换旧列表。
export function mergeMarketBrowsePage(current: SkillMarketplaceBrowse, incoming: SkillMarketplaceBrowse, append: boolean): SkillMarketplaceBrowse {
  return {
    ...incoming,
    items: append ? [...(current.items ?? []), ...(incoming.items ?? [])] : (incoming.items ?? []),
  }
}
