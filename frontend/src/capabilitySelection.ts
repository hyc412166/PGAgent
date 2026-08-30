import type { PermissionMode } from './types'

export const permissionOptions: ReadonlyArray<{ value: PermissionMode; label: string }> = [
  { value: 'ask', label: '请求批准' },
  { value: 'smart', label: '智能审批' },
  { value: 'full', label: '完全访问' },
]

export function normalizedIds(ids: readonly string[]): string[] {
  return [...new Set(ids.map((id) => id.trim()).filter(Boolean))]
}

export function toggleSelectedId(ids: readonly string[], id: string): string[] {
  const normalized = normalizedIds(ids)
  return normalized.includes(id) ? normalized.filter((item) => item !== id) : [...normalized, id]
}

export function permissionLabel(mode: PermissionMode): string {
  return permissionOptions.find((option) => option.value === mode)?.label ?? '智能审批'
}
