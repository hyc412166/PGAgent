// 本模块只负责把连续工具活动整理成展示分组，避免 React 组件文件导出非组件值而破坏热更新边界。
import type { ThoughtActivityItem } from '../../thoughtTimeline'

export type ThoughtActivityPresentationEntry =
  | { kind: 'activity'; id: string; item: ThoughtActivityItem }
  | { kind: 'tool-group'; id: string; items: ThoughtActivityItem[] }

// 没有可见文本的模型步骤只是运输层标记，不应把连续工具调用错误地拆开。
function isInvisibleActivity(item: ThoughtActivityItem) {
  return item.title === 'Tool Search' || (item.kind === 'thought' && !item.detail.trim())
}

// 只合并真正相邻的多次工具调用；有内容的思考、进度和上下文事件仍是分组边界。
export function groupThoughtActivities(items: ThoughtActivityItem[]): ThoughtActivityPresentationEntry[] {
  const entries: ThoughtActivityPresentationEntry[] = []
  let index = 0
  while (index < items.length) {
    const item = items[index]
    if (isInvisibleActivity(item)) {
      index += 1
      continue
    }
    if (item.kind !== 'tool') {
      entries.push({ kind: 'activity', id: item.id, item })
      index += 1
      continue
    }
    const tools: ThoughtActivityItem[] = [item]
    let nextIndex = index + 1
    while (nextIndex < items.length) {
      const nextItem = items[nextIndex]
      if (isInvisibleActivity(nextItem)) {
        nextIndex += 1
        continue
      }
      if (nextItem.kind !== 'tool') break
      tools.push(nextItem)
      nextIndex += 1
    }
    if (tools.length > 1) {
      entries.push({ kind: 'tool-group', id: `tool-group:${tools[0].id}:${tools.at(-1)!.id}`, items: tools })
    } else {
      entries.push({ kind: 'activity', id: item.id, item })
    }
    index = nextIndex
  }
  return entries
}
