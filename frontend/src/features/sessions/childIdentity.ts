import type { DelegatedTask } from '../../types'

export function childNames(tasks: DelegatedTask[]): Record<string, string> {
  const counts = new Map<string, number>()
  const names: Record<string, string> = {}
  // 使用持久化创建顺序，而不是按完成时间排序的列表位置分配编号。
  for (const task of [...tasks].sort((a, b) => (a.created_at || '').localeCompare(b.created_at || '') || a.id.localeCompare(b.id))) {
    const agent = task.result?.agent as { name?: string } | undefined
    const role = task.child_agent_name || agent?.name || '子 Agent'
    const key = task.child_agent_id || role
    const number = (counts.get(key) || 0) + 1
    counts.set(key, number)
    names[task.id] = typeof task.result?.display_name === 'string' && task.result.display_name.trim()
      ? task.result.display_name.trim()
      : `${role} #${number}`
  }
  return names
}
