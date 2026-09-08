// 本文件负责 display 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
// 将可选 ISO 时间转换为本地日期时间，空值或无效值返回占位符。
export function formatDate(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}
// 使用紧凑单位格式化 token 数量。
export function formatTokens(value = 0) {
  return new Intl.NumberFormat('zh-CN', { notation: value >= 10000 ? 'compact' : 'standard', maximumFractionDigits: 2 }).format(value)
}

// 以美元精度格式化模型费用。
export function formatCost(value = 0) {
  return `$${value.toFixed(value >= 1 ? 4 : 6)}`
}

// 将 0 到 1 的比例转换为百分比显示。
export function usageRate(value = 0) {
  const percent = value <= 1 ? value * 100 : value
  return Math.min(100, Math.max(0, percent))
}

// 从不可信接口字段中安全取得字符串 ID。
export function stringId(value: unknown) {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : ''
}
