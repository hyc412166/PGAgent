// 本测试文件验证 sessionNavigation 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'
import { DEFAULT_WORKSPACE_ID, buildSessionNavigation, draftSessionTitle, folderName, mergePendingSession, pendingSessionAfterRemoval, projectRootForSession, sameFolderPath } from './sessionNavigation'

// 测试分组：项目与会话导航。
describe('项目与会话导航', () => {
  const projects = [
    { id: DEFAULT_WORKSPACE_ID, name: 'Default Workspace', root_path: 'C:\\default' },
    { id: 'workspace-a', name: 'Alpha', root_path: 'D:\\work\\alpha' },
  ]
  const sessions = [
    { id: 'task-1', title: '临时任务', workspace_id: DEFAULT_WORKSPACE_ID },
    { id: 'project-1', title: '项目会话', workspace_id: 'workspace-a' },
    { id: 'orphan', title: '无工作区会话' },
  ]

  // 测试场景：把默认工作区会话归入任务并排除默认项目。
  it('把默认工作区会话归入任务并排除默认项目', () => {
    const navigation = buildSessionNavigation(projects, sessions)
    expect(navigation.projects).toHaveLength(1)
    expect(navigation.projects[0].sessions.map((item) => item.id)).toEqual(['project-1'])
    expect(navigation.tasks.map((item) => item.id)).toEqual(['task-1', 'orphan'])
  })

  it('在会话列表刷新前保留刚创建的会话', () => {
    const pending = { id: 'new-session', title: '刚创建的会话' }
    expect(mergePendingSession(sessions, pending).map((item) => item.id)).toEqual(['new-session', 'task-1', 'project-1', 'orphan'])
    expect(mergePendingSession([...sessions, pending], pending).map((item) => item.id)).toEqual(['task-1', 'project-1', 'orphan', 'new-session'])
  })

  it('删除会话或项目后不会重新合并已删除的临时会话', () => {
    const pending = { id: 'new-session', title: '刚创建的会话' }

    expect(pendingSessionAfterRemoval(pending, new Set(['new-session']))).toBeNull()
    expect(pendingSessionAfterRemoval(pending, new Set(['project-1']))).toBe(pending)
    expect(mergePendingSession(sessions, pendingSessionAfterRemoval(pending, new Set(['new-session']))).map((item) => item.id)).toEqual([
      'task-1',
      'project-1',
      'orphan',
    ])
  })

  // 测试场景：生成紧凑文件夹名和草稿标题。
  it('生成紧凑文件夹名和草稿标题', () => {
    expect(folderName('D:\\work\\alpha\\')).toBe('alpha')
    expect(draftSessionTitle('  帮我   分析这个项目  ')).toBe('帮我 分析这个项目')
    expect(draftSessionTitle('a'.repeat(50), 10)).toBe('aaaaaaaaa…')
  })

  // 测试场景：按 Windows 路径语义复用已存在项目。
  it('按 Windows 路径语义复用已存在项目', () => {
    expect(sameFolderPath('D:\\Work\\Alpha\\', 'd:\\work\\alpha')).toBe(true)
    expect(sameFolderPath('', 'd:\\work\\alpha')).toBe(false)
  })

  // 测试场景：仅为项目会话预选新草稿的目录。
  it('仅为项目会话预选新草稿的目录', () => {
    expect(projectRootForSession(projects, { workspace_id: 'workspace-a' })).toBe('D:\\work\\alpha')
    expect(projectRootForSession(projects, { workspace_id: DEFAULT_WORKSPACE_ID })).toBe('')
    expect(projectRootForSession(projects, { workspace_id: 'missing-workspace' })).toBe('')
    expect(projectRootForSession(projects)).toBe('')
  })
})
