import { describe, expect, it } from 'vitest'
import { currentSessionGit, gitStatusLabel } from './gitStatus'

describe('会话 Git 显示', () => {
  const detached = { cwd: '/worktrees/a', git_info: { branch: null, commit_hash: 'abc123456789' }, git_error: null }
  const response = { sessionId: 'session-a', requestedCwd: '/worktrees/a', snapshot: detached }

  it('切换会话或 cwd 时不展示旧 checkout 的分支', () => {
    expect(currentSessionGit(response, 'session-b', '/worktrees/a')).toBeNull()
    expect(currentSessionGit(response, 'session-a', '/projects/main')).toBeNull()
    expect(currentSessionGit(response, 'session-a', '/worktrees/a')).toBe(detached)
  })

  it('普通目录、错误和加载状态不会伪造 main 或 master', () => {
    expect(gitStatusLabel(null)).toBe('非 Git 项目')
    expect(gitStatusLabel({ branch: 'master' }, 'Permission denied')).toBe('Git 读取失败')
    expect(gitStatusLabel({ branch: 'master' }, null, true)).toBe('正在读取 Git…')
    expect(gitStatusLabel(detached.git_info)).toBe('detached HEAD · abc12345')
    expect(gitStatusLabel({ branch: 'feature/new' })).toBe('feature/new')
  })
})
