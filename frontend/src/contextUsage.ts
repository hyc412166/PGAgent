import type { SessionContext } from './types'

export type ContextTone = 'normal' | 'near-limit' | 'compacting'

export interface ContextUsageView {
  percent: number
  roundedPercent: number
  thresholdPercent: number
  tone: ContextTone
  ariaLabel: string
  detail: string
  compressionHint: string
}

export function buildContextUsageView(context: SessionContext): ContextUsageView {
  const percent = context.limit_tokens > 0
    ? Math.min(100, Math.max(0, (context.used_tokens / context.limit_tokens) * 100))
    : 0
  const thresholdPercent = context.limit_tokens > 0
    ? Math.min(100, Math.max(0, (context.compact_threshold_tokens / context.limit_tokens) * 100))
    : 90
  const roundedPercent = Math.round(percent)
  const tone: ContextTone = percent >= thresholdPercent
    ? 'compacting'
    : percent >= Math.max(0, thresholdPercent - 10)
      ? 'near-limit'
      : 'normal'
  const detail = `已用 ${context.used_tokens.toLocaleString('zh-CN')} Token（标记），共 ${context.limit_tokens.toLocaleString('zh-CN')}`
  const compressionHint = percent >= thresholdPercent
    ? `已达到 ${Math.round(thresholdPercent)}%，正在使用自动压缩机制`
    : `达到 ${Math.round(thresholdPercent)}% 时自动压缩上下文`
  return {
    percent,
    roundedPercent,
    thresholdPercent,
    tone,
    ariaLabel: `上下文已使用 ${roundedPercent}%。${detail}。${compressionHint}。`,
    detail,
    compressionHint,
  }
}
