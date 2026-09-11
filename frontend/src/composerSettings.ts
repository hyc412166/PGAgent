// 本文件负责 composerSettings 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { ThinkingLevel } from './types'

// thinkingLevelLabels 为所有思考等级提供统一界面名称。
export const thinkingLevelLabels: Record<ThinkingLevel, string> = {
  low: '低',
  medium: '中',
  high: '高',
  xhigh: '极高',
}

// 用户侧只提交后端明确支持的四档强度，页面展示值与持久化值保持一致。
export const sessionThinkingOptions: ReadonlyArray<{ value: ThinkingLevel; label: string; hint?: string }> = [
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
  { value: 'xhigh', label: '极高', hint: '更快消耗使用额度' },
]

// 按会话、Agent、连接默认值的优先级解析实际思考等级。
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

// 缩短模型名称用于编辑器按钮，完整名称仍由 title 等位置保留。
export function shortModelLabel(model?: string | null, maxLength = 24): string {
  if (!model) return '默认模型'
  const tail = model.split('/').filter(Boolean).at(-1) || model
  return tail.length > maxLength ? `${tail.slice(0, maxLength - 1)}…` : tail
}

// 将选择器的 connection::model 组合值拆成后端会话字段；空值代表自动选择。
export function modelSelectionPayload(value: string): { model_connection_id: string | null; model_id: string | null } {
  if (!value) return { model_connection_id: null, model_id: null }
  const [connectionId, ...modelParts] = value.split('::')
  return { model_connection_id: connectionId, model_id: modelParts.join('::') }
}
