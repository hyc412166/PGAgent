import type { McpServer } from './types'

export function mcpStatusLabel(server: McpServer) {
  if (!server.enabled) return '已停用'
  if (server.runtime_status === 'ready') return '已连接'
  if (server.runtime_status === 'degraded') return '连接异常'
  if (server.runtime_status === 'failed') return '连接失败'
  return '等待会话连接'
}

export function filterMcpServers(servers: McpServer[], query: string) {
  const normalized = query.trim().toLocaleLowerCase()
  if (!normalized) return servers
  return servers.filter((server) =>
    [server.name, server.command, ...server.args].some((value) => value.toLocaleLowerCase().includes(normalized)),
  )
}
