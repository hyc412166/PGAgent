// 本文件负责 modelSettings 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { AgentProfile, Connection, Session } from './types'

// healthyStatuses 定义自动连接排序时视为健康的后端状态别名。
const healthyStatuses = new Set(['connected', 'healthy', 'online'])

// 将连接最后检查时间规范为可排序时间戳，无效时间排到末尾。
function checkedAt(connection: Connection) {
  const value = connection.last_checked_at || connection.last_checked
  const timestamp = value ? new Date(value).getTime() : 0
  return Number.isNaN(timestamp) ? 0 : timestamp
}

// 自动选择优先级为：已启用、健康、最近检查；完全相同时保持原列表顺序。
export function chooseAutomaticConnection(connections: Connection[]): Connection | undefined {
  return connections
    .map((connection, index) => ({ connection, index }))
    .filter(({ connection }) => connection.enabled !== false)
    .sort((left, right) => {
      const healthDifference = Number(healthyStatuses.has((right.connection.status || '').toLowerCase())) - Number(healthyStatuses.has((left.connection.status || '').toLowerCase()))
      if (healthDifference) return healthDifference
      const checkedDifference = checkedAt(right.connection) - checkedAt(left.connection)
      return checkedDifference || left.index - right.index
    })[0]?.connection
}

// 合并默认、发现和手动模型并去重，默认模型始终排在首位。
export function availableConnectionModels(connection?: Connection): string[] {
  if (!connection) return []
  return Array.from(new Set([
    ...(connection.default_model ? [connection.default_model] : []),
    ...(connection.models || []),
    ...(connection.discovered_models || []),
    ...(connection.manual_models || []),
  ]))
}

// 按“会话覆盖 > Agent 默认 > 自动连接”解析本轮实际连接和模型，并返回选择器所需值。
export function resolveEffectiveModelSettings(
  connections: Connection[],
  session?: Session,
  agent?: AgentProfile,
) {
  const agentConnectionId = agent?.model_connection_id || agent?.connection_id || ''
  const sessionConnection = session?.model_connection_id
    ? connections.find((item) => item.id === session.model_connection_id && item.enabled !== false)
    : undefined
  const agentConnection = agentConnectionId
    ? connections.find((item) => item.id === agentConnectionId && item.enabled !== false)
    : undefined
  const automaticConnection = chooseAutomaticConnection(connections)
  const connection = sessionConnection || agentConnection || automaticConnection
  const connectionId = connection?.id || ''

  const sessionModel = session?.model_id && (!session.model_connection_id || session.model_connection_id === connectionId)
    ? session.model_id
    : ''
  const agentModelId = agent?.model_id || agent?.model || ''
  const agentModel = agentModelId && (!agentConnectionId || agentConnectionId === connectionId) ? agentModelId : ''
  const model = sessionModel || agentModel || availableConnectionModels(connection)[0] || ''

  const fallbackConnection = agentConnection || automaticConnection
  const fallbackAgentModel = agentModelId && (!agentConnectionId || agentConnectionId === fallbackConnection?.id) ? agentModelId : ''
  const fallbackModel = fallbackAgentModel || availableConnectionModels(fallbackConnection)[0] || ''
  const sessionConnectionMatches = !session?.model_connection_id || session.model_connection_id === connectionId
  const hasUsableSessionOverride = sessionConnectionMatches && Boolean(session?.model_connection_id || session?.model_id)

  return {
    connection,
    connectionId,
    model,
    selectedValue: hasUsableSessionOverride && model && connectionId ? `${connectionId}::${model}` : '',
    automaticConnection: fallbackConnection,
    automaticModel: fallbackModel,
  }
}
