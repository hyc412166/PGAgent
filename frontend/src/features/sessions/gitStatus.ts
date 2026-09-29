import type { GitInfo } from '../../types'

export type GitSnapshot = { cwd: string; git_info: GitInfo | null; git_error: string | null }
export type SessionGitSnapshot = { sessionId: string; requestedCwd: string; snapshot: GitSnapshot }

// 分支只能来自当前会话目录；项目 checkout 的分支不能替代 worktree 的状态。
export function currentSessionGit(data: SessionGitSnapshot | null, sessionId: string, cwd: string): GitSnapshot | null {
  return data?.sessionId === sessionId && data.requestedCwd === cwd ? data.snapshot : null
}

export function gitStatusLabel(info?: GitInfo | null, error?: string | null, loading = false): string {
  if (loading) return '正在读取 Git…'
  if (error) return 'Git 读取失败'
  if (!info) return '非 Git 项目'
  return info.branch || (info.commit_hash ? `detached HEAD · ${info.commit_hash.slice(0, 8)}` : '未提交的仓库')
}
