import type { RunEvent } from '../../types'

// 同一条助手消息的增量与完成事件合并，避免逐 token 气泡或终态重复正文。
export function childReplies(events: RunEvent[]) {
  const items = new Map<string, string>()
  const generations = new Map<string, number>()
  for (const event of [...events].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0))) {
    const type = event.event_type || event.type
    if (!type?.startsWith('assistant_message_')) continue
    const p = event.payload || {}
    const base = `${p.response_id ?? event.step ?? ''}:${p.item_id ?? p.output_index ?? 0}`
    const identified = Boolean(p.response_id || p.item_id)
    const key = identified ? base : `${base}:${generations.get(base) ?? 0}`
    if (typeof p.content === 'string') items.set(key, p.content)
    else if (typeof p.delta === 'string') items.set(key, (items.get(key) || '') + p.delta)
    if (!identified && type === 'assistant_message_completed') generations.set(base, (generations.get(base) ?? 0) + 1)
  }
  return [...items].filter(([, content]) => content.trim()).map(([id, content]) => ({ id, content }))
}
