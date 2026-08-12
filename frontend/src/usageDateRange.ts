export type UsageDatePreset = 'today' | '24h' | '7d' | '14d' | '30d' | 'custom'

export type QuickUsageDatePreset = Exclude<UsageDatePreset, 'custom'>

export const usageDateOptions: Array<{ id: QuickUsageDatePreset; label: string }> = [
  { id: 'today', label: '今天' },
  { id: '24h', label: '1 天' },
  { id: '7d', label: '7 天' },
  { id: '14d', label: '14 天' },
  { id: '30d', label: '30 天' },
]

const relativeRangeMilliseconds: Record<Exclude<QuickUsageDatePreset, 'today'>, number> = {
  '24h': 24 * 60 * 60 * 1000,
  '7d': 7 * 24 * 60 * 60 * 1000,
  '14d': 14 * 24 * 60 * 60 * 1000,
  '30d': 30 * 24 * 60 * 60 * 1000,
}

export function usageDateKey(date: Date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function usageDatePresetBounds(preset: QuickUsageDatePreset, anchor: Date) {
  const end = new Date(anchor)
  if (preset === 'today') {
    const start = new Date(anchor)
    start.setHours(0, 0, 0, 0)
    return { start, end }
  }
  return { start: new Date(anchor.getTime() - relativeRangeMilliseconds[preset]), end }
}

export function usageDateRange(startDate: string, endDate: string, preset: UsageDatePreset, anchor: Date) {
  let start: Date
  let end: Date
  if (preset === 'custom') {
    start = new Date(`${startDate}T00:00:00`)
    end = new Date(`${endDate}T23:59:59.999`)
  } else {
    ({ start, end } = usageDatePresetBounds(preset, anchor))
  }
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || start > end) return ''
  const params = new URLSearchParams({ start_at: start.toISOString(), end_at: end.toISOString() })
  return `?${params.toString()}`
}

export function usageRangeLabel(startDate: string, endDate: string, preset: UsageDatePreset) {
  if (preset === 'today') return '今天 00:00 至当前'
  if (preset === '24h') return '过去 24 小时'
  if (preset === '7d') return '过去 7 天'
  if (preset === '14d') return '过去 14 天'
  if (preset === '30d') return '过去 30 天'
  return startDate === endDate ? startDate : `${startDate} 至 ${endDate}`
}
