import { draftSessionTitle } from './sessionNavigation'
import type { PermissionMode, ThinkingLevel } from './types'

export interface DraftLaunchSettings {
  model_connection_id: string | null
  model_id: string | null
  thinking_level: ThinkingLevel
  skill_ids: string[]
  permission_mode: PermissionMode
}

export interface DraftLaunchPayload {
  idempotency_key: string
  title: string
  content: string
  root_path?: string
  model_connection_id?: string
  model_id?: string
  thinking_level: ThinkingLevel
  skill_ids: string[]
  permission_mode: PermissionMode
}

export function createDraftIdempotencyKey(randomId?: () => string): string {
  const id = randomId?.()
    ?? globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return `draft-${id}`
}

export function createTurnIdempotencyKey(randomId?: () => string): string {
  const id = randomId?.()
    ?? globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return `turn-${id}`
}

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
    permission_mode: settings.permission_mode,
  }
}
