import type { AgentProfile, Connection, Session } from './types'

const healthyStatuses = new Set(['connected', 'healthy', 'online'])

function checkedAt(connection: Connection) {
  const value = connection.last_checked_at || connection.last_checked
  const timestamp = value ? new Date(value).getTime() : 0
  return Number.isNaN(timestamp) ? 0 : timestamp
}

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

export function availableConnectionModels(connection?: Connection): string[] {
  if (!connection) return []
  return Array.from(new Set([
    ...(connection.default_model ? [connection.default_model] : []),
    ...(connection.models || []),
    ...(connection.discovered_models || []),
    ...(connection.manual_models || []),
  ]))
}

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
