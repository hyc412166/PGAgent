import type { Session, Workspace } from './types'

export const DEFAULT_WORKSPACE_ID = '00000000-0000-0000-0000-000000000001'

export interface ProjectSessionGroup {
  workspace: Workspace
  sessions: Session[]
}

export interface SessionNavigation {
  projects: ProjectSessionGroup[]
  tasks: Session[]
}

export function isDefaultWorkspace(workspace: Pick<Workspace, 'id' | 'name'>): boolean {
  return workspace.id === DEFAULT_WORKSPACE_ID || workspace.name.trim().toLowerCase() === 'default workspace'
}

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

/**
 * Return the selected project directory for an existing project conversation.
 * One-off tasks intentionally return an empty string so a new draft remains a
 * default-workspace task unless the user explicitly picks a directory.
 */
export function projectRootForSession(
  workspaces: Workspace[],
  session?: Pick<Session, 'workspace_id'>,
): string {
  if (!session?.workspace_id) return ''
  const workspace = workspaces.find((item) => item.id === session.workspace_id)
  if (!workspace || isDefaultWorkspace(workspace)) return ''
  return (workspace.root_path || workspace.path || '').trim()
}

export function folderName(path: string): string {
  const normalized = path.trim().replace(/[\\/]+$/, '')
  return normalized.split(/[\\/]/).filter(Boolean).at(-1) || normalized || '所选项目'
}

export function draftSessionTitle(content: string, maxLength = 42): string {
  const compact = content.trim().replace(/\s+/g, ' ')
  if (!compact) return '新对话'
  return compact.length > maxLength ? `${compact.slice(0, maxLength - 1)}…` : compact
}

export function sameFolderPath(left?: string, right?: string): boolean {
  const normalize = (value = '') => value.trim().replace(/[\\/]+$/, '').toLowerCase()
  return Boolean(normalize(left)) && normalize(left) === normalize(right)
}
