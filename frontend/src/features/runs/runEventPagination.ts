import type { RunEventPage } from '../../types'

// 游标页按接口返回顺序追加，并采用新页提供的下一游标。
export function appendRunEventPage(current: RunEventPage, incoming: RunEventPage): RunEventPage {
  const seen = new Set<string>()
  const items = [...current.items, ...incoming.items].filter((event) => {
    const key = event.id || `${event.sequence ?? ''}:${event.created_at ?? ''}:${event.event_type ?? event.type ?? ''}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
  return { items, next_before: incoming.next_before }
}
