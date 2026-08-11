import { describe, expect, it } from 'vitest'
import { DEFAULT_WORKSPACE_ID, buildSessionNavigation, draftSessionTitle, folderName, projectRootForSession, sameFolderPath } from './sessionNavigation'

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

  it('把默认工作区会话归入任务并排除默认项目', () => {
    const navigation = buildSessionNavigation(projects, sessions)
    expect(navigation.projects).toHaveLength(1)
    expect(navigation.projects[0].sessions.map((item) => item.id)).toEqual(['project-1'])
    expect(navigation.tasks.map((item) => item.id)).toEqual(['task-1', 'orphan'])
  })

  it('生成紧凑文件夹名和草稿标题', () => {
    expect(folderName('D:\\work\\alpha\\')).toBe('alpha')
    expect(draftSessionTitle('  帮我   分析这个项目  ')).toBe('帮我 分析这个项目')
    expect(draftSessionTitle('a'.repeat(50), 10)).toBe('aaaaaaaaa…')
  })

  it('按 Windows 路径语义复用已存在项目', () => {
    expect(sameFolderPath('D:\\Work\\Alpha\\', 'd:\\work\\alpha')).toBe(true)
    expect(sameFolderPath('', 'd:\\work\\alpha')).toBe(false)
  })

  it('仅为项目会话预选新草稿的目录', () => {
    expect(projectRootForSession(projects, { workspace_id: 'workspace-a' })).toBe('D:\\work\\alpha')
    expect(projectRootForSession(projects, { workspace_id: DEFAULT_WORKSPACE_ID })).toBe('')
    expect(projectRootForSession(projects, { workspace_id: 'missing-workspace' })).toBe('')
    expect(projectRootForSession(projects)).toBe('')
  })
})
