// 本文件负责 usageDateRange 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
// UsageDatePreset 覆盖快捷时间窗和用户自定义日期范围。
export type UsageDatePreset = 'today' | '24h' | '7d' | '14d' | '30d' | 'custom'

export type QuickUsageDatePreset = Exclude<UsageDatePreset, 'custom'>

// usageDateOptions 定义快捷筛选器的顺序和标签。
export const usageDateOptions: Array<{ id: QuickUsageDatePreset; label: string }> = [
  { id: 'today', label: '今天' },
  { id: '24h', label: '1 天' },
  { id: '7d', label: '7 天' },
  { id: '14d', label: '14 天' },
  { id: '30d', label: '30 天' },
]

// 非“今天”预设按相对毫秒数回推起点。
const relativeRangeMilliseconds: Record<Exclude<QuickUsageDatePreset, 'today'>, number> = {
  '24h': 24 * 60 * 60 * 1000,
  '7d': 7 * 24 * 60 * 60 * 1000,
  '14d': 14 * 24 * 60 * 60 * 1000,
  '30d': 30 * 24 * 60 * 60 * 1000,
}

// 将本地日期格式化为日期输入框和查询接口共用的 YYYY-MM-DD。
export function usageDateKey(date: Date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

// 以 anchor 为固定基准计算快捷范围，避免一次渲染内跨秒产生边界漂移。
export function usageDatePresetBounds(preset: QuickUsageDatePreset, anchor: Date) {
  const end = new Date(anchor)
  if (preset === 'today') {
    const start = new Date(anchor)
    start.setHours(0, 0, 0, 0)
    return { start, end }
  }
  return { start: new Date(anchor.getTime() - relativeRangeMilliseconds[preset]), end }
}

// 生成后端用量接口的查询字符串，自定义范围按完整自然日闭区间处理。
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

// 生成当前用量范围的简短展示标签。
export function usageRangeLabel(startDate: string, endDate: string, preset: UsageDatePreset) {
  if (preset === 'today') return '今天 00:00 至当前'
  if (preset === '24h') return '过去 24 小时'
  if (preset === '7d') return '过去 7 天'
  if (preset === '14d') return '过去 14 天'
  if (preset === '30d') return '过去 30 天'
  return startDate === endDate ? startDate : `${startDate} 至 ${endDate}`
}
