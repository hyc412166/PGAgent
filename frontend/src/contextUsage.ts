// 本文件负责 contextUsage 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { SessionContext } from './types'

// ContextTone 驱动上下文环的普通、临界和压缩中视觉状态。
export type ContextTone = 'normal' | 'near-limit' | 'compacting'

// ContextUsageView 是由原始 token 数据派生的纯展示模型。
export interface ContextUsageView {
  percent: number
  roundedPercent: number
  thresholdPercent: number
  tone: ContextTone
  ariaLabel: string
  detail: string
  compressionHint: string
}

// 规范化上限、阈值和百分比，并生成环形进度所需角度与提示文案。
export function buildContextUsageView(context: SessionContext): ContextUsageView {
  const usedTokens = context.active_context_tokens ?? context.used_tokens
  const limitTokens = context.full_context_window_limit ?? context.limit_tokens
  const compactLimit = context.auto_compact_scope_limit ?? context.compact_threshold_tokens
  const percent = limitTokens > 0
    ? Math.min(100, Math.max(0, (usedTokens / limitTokens) * 100))
    : 0
  const thresholdPercent = limitTokens > 0
    ? Math.min(100, Math.max(0, (compactLimit / limitTokens) * 100))
    : 90
  const roundedPercent = Math.round(percent)
  const tone: ContextTone = percent >= thresholdPercent
    ? 'compacting'
    : percent >= Math.max(0, thresholdPercent - 10)
      ? 'near-limit'
      : 'normal'
  const remainingTokens = context.base_window_tokens_remaining ?? Math.max(0, limitTokens - usedTokens)
  const detail = `已用 ${usedTokens.toLocaleString('zh-CN')} Token，共 ${limitTokens.toLocaleString('zh-CN')}；剩余 ${remainingTokens.toLocaleString('zh-CN')} Token`
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
