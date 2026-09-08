// 本文件负责 mcpPresentation 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { McpServer } from './types'

// 将 MCP 启用及连接状态映射为卡片标签。
export function mcpStatusLabel(server: McpServer) {
  if (!server.enabled) return '已停用'
  if (server.runtime_status === 'dormant') return '缓存就绪，等待调用'
  if (server.runtime_status === 'ready') return '已连接'
  if (server.runtime_status === 'degraded') return '连接异常'
  if (server.runtime_status === 'failed') return '连接失败'
  return '等待会话连接'
}

// 按名称、命令和参数执行不区分大小写的本地过滤。
export function filterMcpServers(servers: McpServer[], query: string) {
  const normalized = query.trim().toLocaleLowerCase()
  if (!normalized) return servers
  return servers.filter((server) =>
    [server.name, server.command, ...server.args].some((value) => value.toLocaleLowerCase().includes(normalized)),
  )
}
