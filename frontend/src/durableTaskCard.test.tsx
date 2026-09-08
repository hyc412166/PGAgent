// 本测试文件验证 durableTaskCard 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { DurableTaskCard } from './features/sessions/components/DurableTaskCard'
import type { DurableTask } from './types'

const pausedTask: DurableTask = {
  id: 'task-1',
  session_id: 'session-1',
  goal: 'Use Playwright MCP',
  status: 'paused',
  steps: [{
    id: 'step-1',
    external_id: 'connect',
    position: 1,
    title: 'Connect Playwright',
    status: 'needs_recovery',
    executor_kind: 'main',
    workspace_mode: 'shared',
  }],
}

// 测试分组：durable task card。
describe('durable task card', () => {
  // 测试场景：offers resume and durable cancellation for a paused task。
  it('offers resume and durable cancellation for a paused task', () => {
    const markup = renderToStaticMarkup(
      <DurableTaskCard task={pausedTask} onResume={() => undefined} onCancel={() => undefined} />,
    )

    expect(markup).toContain('继续此任务')
    expect(markup).toContain('取消任务')
  })

  // 测试场景：locks both task actions while cancellation is being persisted。
  it('locks both task actions while cancellation is being persisted', () => {
    const markup = renderToStaticMarkup(
      <DurableTaskCard task={pausedTask} onResume={() => undefined} onCancel={() => undefined} cancelling />,
    )

    expect(markup.match(/disabled=""/g)).toHaveLength(2)
    expect(markup).toContain('正在取消…')
  })

  // 测试场景：shows direct resume progress while the recovery request is being sent。
  it('shows direct resume progress while the recovery request is being sent', () => {
    const markup = renderToStaticMarkup(
      <DurableTaskCard task={pausedTask} onResume={() => undefined} onCancel={() => undefined} resuming />,
    )

    expect(markup).toContain('正在继续…')
    expect(markup).toContain('aria-busy="true"')
    expect(markup.match(/disabled=""/g)).toHaveLength(1)
  })

  // 测试场景：can cancel a running task without offering a duplicate resume action。
  it('can cancel a running task without offering a duplicate resume action', () => {
    const markup = renderToStaticMarkup(
      <DurableTaskCard task={{ ...pausedTask, status: 'running' }} onResume={() => undefined} onCancel={() => undefined} />,
    )

    expect(markup).toContain('取消任务')
    expect(markup).not.toContain('继续此任务')
  })
})
