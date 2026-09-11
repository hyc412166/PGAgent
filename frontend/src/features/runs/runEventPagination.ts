import type { RunEventPage } from '../../types'

// 游标页按接口返回顺序追加，并采用新页提供的下一游标。
export function appendRunEventPage(current: RunEventPage, incoming: RunEventPage): RunEventPage {
  return { items: [...current.items, ...incoming.items], next_before: incoming.next_before }
}
