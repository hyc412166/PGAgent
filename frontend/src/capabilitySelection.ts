// 本文件负责 capabilitySelection 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { PermissionMode } from './types'

// permissionOptions 是权限菜单展示顺序及枚举值的唯一映射。
export const permissionOptions: ReadonlyArray<{ value: PermissionMode; label: string }> = [
  { value: 'ask', label: '请求批准' },
  { value: 'smart', label: '智能审批' },
  { value: 'full', label: '完全访问' },
]

// 去除空值和重复 ID，并排序生成稳定的能力配置。
export function normalizedIds(ids: readonly string[]): string[] {
  return [...new Set(ids.map((id) => id.trim()).filter(Boolean))]
}

// 在不可变数组中切换指定能力 ID，并返回规范化结果。
export function toggleSelectedId(ids: readonly string[], id: string): string[] {
  const normalized = normalizedIds(ids)
  return normalized.includes(id) ? normalized.filter((item) => item !== id) : [...normalized, id]
}

// 将权限枚举转换为菜单标签。
export function permissionLabel(mode: PermissionMode): string {
  return permissionOptions.find((option) => option.value === mode)?.label ?? '智能审批'
}
