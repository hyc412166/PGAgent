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

describe('durable task card', () => {
  it('offers resume and durable cancellation for a paused task', () => {
    const markup = renderToStaticMarkup(
      <DurableTaskCard task={pausedTask} onResume={() => undefined} onCancel={() => undefined} />,
    )

    expect(markup).toContain('继续此任务')
    expect(markup).toContain('取消任务')
  })

  it('locks both task actions while cancellation is being persisted', () => {
    const markup = renderToStaticMarkup(
      <DurableTaskCard task={pausedTask} onResume={() => undefined} onCancel={() => undefined} cancelling />,
    )

    expect(markup.match(/disabled=""/g)).toHaveLength(2)
    expect(markup).toContain('正在取消…')
  })

  it('can cancel a running task without offering a duplicate resume action', () => {
    const markup = renderToStaticMarkup(
      <DurableTaskCard task={{ ...pausedTask, status: 'running' }} onResume={() => undefined} onCancel={() => undefined} />,
    )

    expect(markup).toContain('取消任务')
    expect(markup).not.toContain('继续此任务')
  })
})
