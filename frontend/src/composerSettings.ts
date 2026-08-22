import type { ThinkingLevel } from './types'

export const thinkingLevelLabels: Record<ThinkingLevel, string> = {
  off: '关闭',
  auto: '自动',
  low: '低',
  medium: '中',
  high: '高',
  xhigh: '极高',
}

export function resolveEffectiveThinking(
  sessionLevel?: string | null,
  agentLevel?: string | null,
  connectionLevel?: string | null,
): ThinkingLevel {
  for (const level of [sessionLevel, agentLevel, connectionLevel]) {
    if (level && ['low', 'medium', 'high', 'xhigh'].includes(level)) return level as ThinkingLevel
  }
  return 'medium'
}

export function shortModelLabel(model?: string | null, maxLength = 24): string {
  if (!model) return '默认模型'
  const tail = model.split('/').filter(Boolean).at(-1) || model
  return tail.length > maxLength ? `${tail.slice(0, maxLength - 1)}…` : tail
}

export function modelSelectionPayload(value: string): { model_connection_id: string | null; model_id: string | null } {
  if (!value) return { model_connection_id: null, model_id: null }
  const [connectionId, ...modelParts] = value.split('::')
  return { model_connection_id: connectionId, model_id: modelParts.join('::') }
}
