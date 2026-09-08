// 本测试文件验证 mcpLiveProgress 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { LiveAssistantMessage } from './features/sessions/presentation'
import { emptyThoughtTimeline, updateThoughtTimeline } from './thoughtTimeline'

// 测试分组：MCP live progress。
describe('MCP live progress', () => {
  // 测试场景：shows the real connection phase instead of hiding it behind a generic thinking phrase。
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
    expect(markup).toContain('执行详情')
    expect(markup).toContain('playwright')
    expect(markup).not.toContain('通用思考文案')
  })
})
