// 本文件负责 sessionNavigation 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { Session, Workspace } from './types'

// DEFAULT_WORKSPACE_ID 标识不属于用户项目目录的系统默认工作区。
export const DEFAULT_WORKSPACE_ID = '00000000-0000-0000-0000-000000000001'

// ProjectSessionGroup 是项目树一层工作区及其子会话的展示结构。
export interface ProjectSessionGroup {
  workspace: Workspace
  sessions: Session[]
}

// SessionNavigation 将项目会话与默认工作区的普通会话分开。
export interface SessionNavigation {
  projects: ProjectSessionGroup[]
  tasks: Session[]
}

// 同时兼容固定 ID 和历史名称识别系统默认工作区。
export function isDefaultWorkspace(workspace: Pick<Workspace, 'id' | 'name'>): boolean {
  return workspace.id === DEFAULT_WORKSPACE_ID || workspace.name.trim().toLowerCase() === 'default workspace'
}

// 按 workspace_id 将会话分组，未匹配或默认工作区会话归入普通对话。
export function buildSessionNavigation(workspaces: Workspace[], sessions: Session[]): SessionNavigation {
  const projects = workspaces.filter((workspace) => !isDefaultWorkspace(workspace))
  return {
    projects: projects.map((workspace) => ({
      workspace,
      sessions: sessions.filter((session) => session.workspace_id === workspace.id),
    })),
    tasks: sessions.filter((session) => !session.workspace_id || session.workspace_id === DEFAULT_WORKSPACE_ID),
  }
}

// 首轮启动接口先返回会话、列表刷新稍后才完成；临时合并该会话避免页面退回空状态。
export function mergePendingSession(sessions: Session[], pending?: Session | null): Session[] {
  if (!pending || sessions.some((session) => session.id === pending.id)) return sessions
  return [pending, ...sessions]
}

// 删除会话或整个项目时同步废弃对应临时会话，避免列表刷新后重新插入后端已不存在的条目。
export function pendingSessionAfterRemoval(pending: Session | null, removedSessionIds: ReadonlySet<string>): Session | null {
  return pending && removedSessionIds.has(String(pending.id)) ? null : pending
}

/**
 * Return the selected project directory for an existing project conversation.
 * One-off tasks intentionally return an empty string so a new draft remains a
 * default-workspace task unless the user explicitly picks a directory.
 */
// 返回会话所属的真实项目根目录；默认工作区返回空字符串。
export function projectRootForSession(
  workspaces: Workspace[],
  session?: Pick<Session, 'workspace_id'>,
): string {
  if (!session?.workspace_id) return ''
  const workspace = workspaces.find((item) => item.id === session.workspace_id)
  if (!workspace || isDefaultWorkspace(workspace)) return ''
  return (workspace.root_path || workspace.path || '').trim()
}

// 从 Windows 或 POSIX 路径中提取末级目录名。
export function folderName(path: string): string {
  const normalized = path.trim().replace(/[\\/]+$/, '')
  return normalized.split(/[\\/]/).filter(Boolean).at(-1) || normalized || '所选项目'
}

// 从首轮输入生成紧凑会话标题，并折叠空白字符。
export function draftSessionTitle(content: string, maxLength = 42): string {
  const compact = content.trim().replace(/\s+/g, ' ')
  if (!compact) return '新对话'
  return compact.length > maxLength ? `${compact.slice(0, maxLength - 1)}…` : compact
}

// 规范化分隔符、末尾斜杠和大小写后比较两个文件夹路径。
export function sameFolderPath(left?: string, right?: string): boolean {
  const normalize = (value = '') => value.trim().replace(/[\\/]+$/, '').toLowerCase()
  return Boolean(normalize(left)) && normalize(left) === normalize(right)
}
