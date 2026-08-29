import { describe, expect, it } from 'vitest'
import { filterMcpServers, mcpStatusLabel } from './mcpPresentation'
import type { McpServer } from './types'

const server: McpServer = {
  name: 'filesystem',
  command: 'npx',
  args: ['-y', '@modelcontextprotocol/server-filesystem'],
  enabled: true,
  transport: 'stdio',
  runtime_status: 'inactive',
  tool_count: 0,
}

describe('MCP workbench presentation', () => {
  it('searches names, commands and arguments', () => {
    expect(filterMcpServers([server], 'FILES')).toEqual([server])
    expect(filterMcpServers([server], 'modelcontextprotocol')).toEqual([server])
    expect(filterMcpServers([server], 'python')).toEqual([])
  })

  it('prioritizes the disabled state over runtime state', () => {
    expect(mcpStatusLabel({ ...server, enabled: false, runtime_status: 'ready' })).toBe('已停用')
    expect(mcpStatusLabel({ ...server, runtime_status: 'ready' })).toBe('已连接')
  })
})
