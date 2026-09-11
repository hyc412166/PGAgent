// 本测试文件验证 SessionsPage 的会话级界面状态在异步数据刷新和会话切换时不会串用。
import { describe, expect, it } from 'vitest'

import { SessionsPageState } from './SessionsPage'
import type { DelegatedTask, Session } from '../../types'

const { nextSelectedSessionId, resolveActiveSessionId, nextChildPanelStateForTasks, resolveExpandedWorkspaceIds, resolveMenuOpen } = SessionsPageState

describe('SessionsPage 会话级状态', () => {
  it('会话列表首次到达时选择第一项，但草稿模式保持未选择', () => {
    const sessions = [{ id: 'session-a' }, { id: 'session-b' }] as Session[]

    expect(resolveActiveSessionId('', false, sessions)).toBe('session-a')
    expect(resolveActiveSessionId('', true, sessions)).toBe('')
    expect(resolveActiveSessionId('session-b', false, sessions)).toBe('session-b')
    expect(nextSelectedSessionId('', sessions)).toBe('session-a')
    expect(nextSelectedSessionId('session-a', [...sessions].reverse())).toBe('session-a')
  })

  it('子任务首次出现时自动打开，手动关闭持续到任务清空或切换会话', () => {
    const firstTasks = [{ id: 'task-a' }] as DelegatedTask[]
    const nextTasks = [{ id: 'task-a' }, { id: 'task-b' }] as DelegatedTask[]
    const opened = nextChildPanelStateForTasks({ sessionId: '', open: false, autoOpened: false }, 'session-a', firstTasks)
    const closed = { ...opened, open: false }
    const reset = nextChildPanelStateForTasks(closed, 'session-a', [])

    expect(opened).toEqual({ sessionId: 'session-a', open: true, autoOpened: true })
    expect(nextChildPanelStateForTasks(closed, 'session-a', nextTasks).open).toBe(false)
    expect(nextChildPanelStateForTasks(reset, 'session-a', firstTasks).open).toBe(true)
    expect(nextChildPanelStateForTasks(closed, 'session-b', firstTasks).open).toBe(true)
  })

  it('项目展开状态默认跟随当前会话，并保留当前上下文中的手动折叠', () => {
    const collapsed = { contextKey: 'session:session-a', ids: new Set<string>() }

    expect([...resolveExpandedWorkspaceIds(null, 'session:session-a', 'workspace-a')]).toEqual(['workspace-a'])
    expect([...resolveExpandedWorkspaceIds(collapsed, 'session:session-a', 'workspace-a')]).toEqual([])
    expect([...resolveExpandedWorkspaceIds(collapsed, 'session:session-b', 'workspace-b')]).toEqual(['workspace-b'])
  })

  it('菜单只在打开时的会话和运行上下文仍有效且未锁定时展示', () => {
    expect(resolveMenuOpen(true, 'session-a:idle', 'session-a:idle', false)).toBe(true)
    expect(resolveMenuOpen(true, 'session-a:idle', 'session-b:idle', false)).toBe(false)
    expect(resolveMenuOpen(true, 'session-a:idle', 'session-a:run-a', true)).toBe(false)
    expect(resolveMenuOpen(false, 'session-a:idle', 'session-a:idle', false)).toBe(false)
  })
})
