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
export function formatTokens(value = 0) {
  return new Intl.NumberFormat('zh-CN', { notation: value >= 10000 ? 'compact' : 'standard', maximumFractionDigits: 2 }).format(value)
}

export function formatCost(value = 0) {
  return `$${value.toFixed(value >= 1 ? 4 : 6)}`
}

export function usageRate(value = 0) {
  const percent = value <= 1 ? value * 100 : value
  return Math.min(100, Math.max(0, percent))
}

export function stringId(value: unknown) {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : ''
}
