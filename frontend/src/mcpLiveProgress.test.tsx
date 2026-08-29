import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { LiveAssistantMessage } from './features/sessions/presentation'
import { emptyThoughtTimeline, updateThoughtTimeline } from './thoughtTimeline'

describe('MCP live progress', () => {
  it('shows the real connection phase instead of hiding it behind a generic thinking phrase', () => {
    const thought = updateThoughtTimeline(emptyThoughtTimeline, {
      type: 'mcp_connecting',
      servers: ['playwright'],
    }, 1_000)
    const markup = renderToStaticMarkup(<LiveAssistantMessage liveRun={{
      runId: 'run-1',
      phase: '正在连接 MCP 服务…',
      draft: '',
      status: 'live',
      error: '',
      thought,
      thinkingStatus: '通用思考文案',
    }} />)

    expect(markup).toContain('正在连接 MCP 服务…')
    expect(markup).toContain('playwright')
    expect(markup).not.toContain('通用思考文案')
  })
})
