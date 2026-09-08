// 本文件负责 draftLaunch 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import { draftSessionTitle } from './sessionNavigation'
import type { PermissionMode, ThinkingLevel } from './types'

// DraftLaunchSettings 汇集新会话首轮运行可配置的模型、能力和记忆选项。
export interface DraftLaunchSettings {
  model_connection_id: string | null
  model_id: string | null
  thinking_level: ThinkingLevel
  skill_ids: string[]
  mcp_server_names: string[]
  permission_mode: PermissionMode
  use_memories: boolean
}

// DraftLaunchPayload 是创建新会话并立即启动首轮的后端请求结构。
export interface DraftLaunchPayload {
  idempotency_key: string
  title: string
  content: string
  root_path?: string
  model_connection_id?: string
  model_id?: string
  thinking_level: ThinkingLevel
  skill_ids: string[]
  mcp_server_names: string[]
  permission_mode: PermissionMode
  use_memories: boolean
}

// 为新会话启动生成幂等键，允许同一操作安全重试。
export function createDraftIdempotencyKey(randomId?: () => string): string {
  const id = randomId?.()
    ?? globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return `draft-${id}`
}

// 为已有会话的新一轮运行生成独立幂等键。
export function createTurnIdempotencyKey(randomId?: () => string): string {
  const id = randomId?.()
    ?? globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return `turn-${id}`
}

// 将编辑器内容、项目路径和草稿设置组装为完整启动请求。
export function buildDraftLaunchPayload(
  idempotencyKey: string,
  content: string,
  rootPath: string,
  settings: DraftLaunchSettings,
): DraftLaunchPayload {
  const normalizedContent = content.trim()
  const normalizedPath = rootPath.trim()
  return {
    idempotency_key: idempotencyKey,
    title: draftSessionTitle(normalizedContent),
    content: normalizedContent,
    ...(normalizedPath ? { root_path: normalizedPath } : {}),
    ...(settings.model_connection_id ? { model_connection_id: settings.model_connection_id } : {}),
    ...(settings.model_id ? { model_id: settings.model_id } : {}),
    thinking_level: settings.thinking_level,
    skill_ids: [...new Set(settings.skill_ids)],
    mcp_server_names: [...new Set(settings.mcp_server_names)],
    permission_mode: settings.permission_mode,
    use_memories: settings.use_memories,
  }
}
