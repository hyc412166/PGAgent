// 本测试文件验证 mcpPresentation 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
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

// 测试分组：MCP workbench presentation。
describe('MCP workbench presentation', () => {
  // 测试场景：searches names, commands and arguments。
  it('searches names, commands and arguments', () => {
    expect(filterMcpServers([server], 'FILES')).toEqual([server])
    expect(filterMcpServers([server], 'modelcontextprotocol')).toEqual([server])
    expect(filterMcpServers([server], 'python')).toEqual([])
  })

  // 测试场景：prioritizes the disabled state over runtime state。
  it('prioritizes the disabled state over runtime state', () => {
    expect(mcpStatusLabel({ ...server, enabled: false, runtime_status: 'ready' })).toBe('已停用')
    expect(mcpStatusLabel({ ...server, runtime_status: 'dormant' })).toBe('缓存就绪，等待调用')
    expect(mcpStatusLabel({ ...server, runtime_status: 'ready' })).toBe('已连接')
  })
})
