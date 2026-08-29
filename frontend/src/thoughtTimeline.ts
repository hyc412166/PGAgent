import type { RunStreamEvent } from './sessionStream'

export interface ThoughtToolItem {
  id: string
  name: string
  target: string
  status: 'running' | 'completed' | 'failed'
}

/**
 * A user-facing processing item. Tool metadata is sanitized separately;
 * provider reasoning is carried only by the explicit thought stream and is
 * never mixed into the assistant answer or conversation context.
 */
export type ThoughtActivityKind = 'thought' | 'tool' | 'context' | 'approval' | 'task' | 'event'
export type ThoughtActivityIcon = 'think' | 'read' | 'write' | 'edit' | 'search' | 'shell' | 'task' | 'approval' | 'context' | 'generic'

export interface ThoughtActivityItem {
  id: string
  kind: ThoughtActivityKind
  icon: ThoughtActivityIcon
  title: string
  detail: string
  status: 'running' | 'completed' | 'failed'
}

export interface ThoughtTimelineState {
  startedAt: number | null
  elapsedMs: number
  finished: boolean
  tools: ThoughtToolItem[]
  items: ThoughtActivityItem[]
  activeItemId?: string
  conclusion?: string
}

export const emptyThoughtTimeline: ThoughtTimelineState = {
  startedAt: null,
  elapsedMs: 0,
  finished: false,
  tools: [],
  items: [],
  activeItemId: undefined,
}

const thinkingPhrases = [
  '翻抽屉找思路中…',
  '正在召唤灵感中…',
  '和橡皮鸭开会中…',
  '给代码挠痒痒中…',
  '煮咖啡顺便推理中…',
  '打 game 间隙认真思考中…',
  '吃饭中…（其实在算）',
  '假装看窗外，脑内开会中…',
  '把脑细胞排成队中…',
  '和 bug 友好谈判中…',
  '猫咪监工中…（认真版）',
  '搜索宇宙说明书中…',
  '给逻辑系鞋带中…',
  '偷偷搭积木中…',
  '把灵感从沙发缝里捞出中…',
] as const

const thinkingFaces = [
  '(•̀ᴗ•́)و',
  '(｡•̀ᴗ-)✧',
  '(ง •̀_•́)ง',
  '(๑•̀ㅂ•́)و✧',
  '(≧▽≦)ゞ',
  '(｡•̀ᴗ•́｡)',
  'ฅ^•ﻌ•^ฅ',
  '(˶ᵔ ᵕ ᵔ˶)',
  '(๑˃̵ᴗ˂̵)و',
  '(｡•́‿•̀｡)',
] as const

export function pickThinkingStatus(random: () => number = Math.random): string {
  const phraseIndex = Math.min(thinkingPhrases.length - 1, Math.max(0, Math.floor(random() * thinkingPhrases.length)))
  const faceIndex = Math.min(thinkingFaces.length - 1, Math.max(0, Math.floor(random() * thinkingFaces.length)))
  return `${thinkingPhrases[phraseIndex]} ${thinkingFaces[faceIndex]}`
}

export function thinkingStatusForRun(runId: string): string {
  let hash = 2166136261
  for (const character of runId || 'pending') hash = Math.imul(hash ^ character.charCodeAt(0), 16777619)
  let call = 0
  return pickThinkingStatus(() => {
    const shifted = call++ === 0 ? hash : Math.imul(hash ^ 0x9e3779b9, 16777619)
    return (shifted >>> 0) / 0x1_0000_0000
  })
}

const toolStartTypes = new Set(['tool_started', 'tool_call'])
const toolFinishTypes = new Set(['tool_finished', 'tool_result'])
const terminalTypes = new Set(['run_completed', 'completed', 'run_interrupted', 'run_stopped', 'stopped', 'model_failed', 'integration_failed', 'failed'])
const terminalStatuses = new Set(['completed', 'stopped', 'failed', 'cancelled'])

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

function firstString(...values: unknown[]): string {
  return values.find((value) => typeof value === 'string' && value.trim())?.toString().trim() ?? ''
}

export function safeToolTarget(event: RunStreamEvent, maxLength = 72): string {
  const payload = record(event.payload)
  const args = record(event.arguments) ?? record(event.input) ?? record(payload?.arguments) ?? record(payload?.input)
  let target = firstString(
    event.path,
    event.file_path,
    event.url,
    event.target,
    payload?.path,
    payload?.file_path,
    payload?.url,
    payload?.target,
    args?.path,
    args?.file_path,
    args?.url,
  )
  if (!target) return ''

  try {
    const parsed = new URL(target)
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      parsed.search = ''
      parsed.hash = ''
      target = parsed.toString()
    }
  } catch {
    // File paths are intentionally handled as plain text.
  }

  if (target.length <= maxLength) return target
  const head = Math.ceil((maxLength - 1) * 0.58)
  const tail = Math.floor((maxLength - 1) * 0.42)
  return `${target.slice(0, head)}…${target.slice(-tail)}`
}

export function displayToolName(name: string): string {
  const normalized = name.trim().toLowerCase()
  const labels: Record<string, string> = {
    read: 'Read',
    read_file: 'Read',
    write: 'Write',
    write_file: 'Write',
    edit: 'Edit',
    webfetch: 'WebFetch',
    web_fetch: 'WebFetch',
    websearch: 'WebSearch',
    web_search: 'WebSearch',
    bash: 'Shell',
    run_command: 'Shell',
  }
  return (labels[normalized] ?? name.trim()) || 'Tool'
}

export function thoughtIconForTool(name: string): ThoughtActivityIcon {
  const normalized = name.trim().toLowerCase()
  if (normalized === 'task' || normalized.includes('delegate')) return 'task'
  if (normalized.includes('search') || normalized.includes('fetch') || normalized.includes('browse') || normalized.includes('web')) return 'search'
  if (normalized.includes('read') || normalized.includes('list') || normalized.includes('glob') || normalized.includes('file')) return 'read'
  if (normalized.includes('write') || normalized.includes('create') || normalized.includes('delete')) return 'write'
  if (normalized.includes('edit') || normalized.includes('patch') || normalized.includes('replace')) return 'edit'
  if (normalized.includes('bash') || normalized.includes('command') || normalized.includes('shell') || normalized.includes('exec')) return 'shell'
  return 'generic'
}

function cleanActivityText(value: string, maxLength = 180): string {
  const cleaned = [...value].filter((character) => {
    const code = character.charCodeAt(0)
    return !(code <= 8 || code === 11 || code === 12 || (code >= 14 && code <= 31))
  }).join('')
  const normalized = cleaned.replace(/\s+/g, ' ').trim()
  if (normalized.length <= maxLength) return normalized
  return `${normalized.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`
}

function cleanThoughtText(value: string, maxLength = 20_000): string {
  const cleaned = [...value].filter((character) => {
    const code = character.charCodeAt(0)
    return !(code <= 8 || code === 11 || code === 12 || (code >= 14 && code <= 31))
  }).join('').replace(/\r\n?/g, '\n')
  return cleaned.slice(0, maxLength)
}

function thoughtItemId(event: RunStreamEvent, itemIndex: number): string {
  const step = typeof event.step === 'number' ? event.step : undefined
  return step === undefined ? `thought-${itemIndex}` : `thought-step-${step}`
}

/**
 * Read only fields intentionally emitted as safe progress metadata.  In
 * particular, do not fall back to `content`, `delta`, `reasoning` or the raw
 * arguments: those may contain hidden chain-of-thought or secrets.
 */
function safeProgressText(event: RunStreamEvent): string {
  const payload = record(event.payload)
  return cleanActivityText(firstString(
    event.progress,
    event.summary,
    event.status_text,
    event.activity,
    payload?.progress,
    payload?.summary,
    payload?.status_text,
    payload?.activity,
  ), 240)
}

function safeMcpServerNames(event: RunStreamEvent): string {
  const payload = record(event.payload)
  const rawServers = Array.isArray(event.servers)
    ? event.servers
    : Array.isArray(payload?.servers) ? payload.servers : []
  const names = rawServers
    .filter((value): value is string => typeof value === 'string')
    .map((value) => cleanActivityText(value, 48))
    .filter(Boolean)
  if (names.length <= 3) return names.join('、')
  return `${names.slice(0, 3).join('、')} 等 ${names.length} 个服务`
}

function mcpToolCount(event: RunStreamEvent): number {
  const payload = record(event.payload)
  const value = typeof event.tool_count === 'number' ? event.tool_count : payload?.tool_count
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0
}

function verificationReason(event: RunStreamEvent, type: string): string {
  if (!type.startsWith('completion_verification_')) return ''
  const payload = record(event.payload)
  return cleanActivityText(firstString(event.failure_reason, payload?.failure_reason, event.reason, payload?.reason), 240)
}

function stableDelegationActivityId(event: RunStreamEvent, fallback: string): string {
  const payload = record(event.payload)
  const key = firstString(
    event.task_id,
    event.delegation_id,
    event.child_run_id,
    payload?.task_id,
    payload?.delegation_id,
    payload?.child_run_id,
  )
  return key ? `task:${key}` : fallback
}

function safeArgumentDetail(event: RunStreamEvent): string {
  const args = record(event.arguments) ?? record(event.input) ?? record(record(event.payload)?.arguments) ?? record(record(event.payload)?.input)
  if (!args) return ''
  const command = record(args.command)
  if (command?.executable) {
    const count = typeof command.argument_count === 'number' ? command.argument_count : undefined
    return count === undefined ? `执行 ${String(command.executable)}` : `执行 ${String(command.executable)}（${count} 个参数）`
  }
  const query = record(args.query)
  if (query?.chars !== undefined) return `搜索查询（${String(query.chars)} 字符）`
  const task = record(args.task)
  if (task?.chars !== undefined) return `编排子 Agent（任务 ${String(task.chars)} 字符）`
  const remaining = record(args.remaining_call_count)
  if (remaining?.count !== undefined) return `还有 ${String(remaining.count)} 个待处理调用`
  return ''
}

function safeActivityDetail(event: RunStreamEvent, toolName = ''): string {
  const target = safeToolTarget(event)
  const progress = safeProgressText(event)
  if (progress) return progress
  const argumentDetail = safeArgumentDetail(event)
  if (argumentDetail) return argumentDetail
  if (target) return target
  const normalized = toolName.trim().toLowerCase()
  if (normalized.includes('read') || normalized.includes('list') || normalized.includes('glob')) return '读取项目内容'
  if (normalized.includes('write') || normalized.includes('create') || normalized.includes('edit') || normalized.includes('patch')) return '准备修改项目文件'
  if (normalized.includes('search') || normalized.includes('fetch') || normalized.includes('browse') || normalized.includes('web')) return '查找相关资料'
  if (normalized === 'task' || normalized.includes('delegate')) return '编排专长子 Agent'
  if (normalized.includes('bash') || normalized.includes('command') || normalized.includes('shell') || normalized.includes('exec')) return '准备执行命令'
  return ''
}

const safeProgressTypes = new Set(['progress', 'agent_progress', 'thought_summary', 'activity_update'])
const contextActivityTypes = new Set(['context_prepared', 'context_resumed', 'context_compacted', 'context_compaction_started', 'context_compaction_finished', 'context_compaction_failed'])

function activityFromNonToolEvent(event: RunStreamEvent, itemIndex: number): ThoughtActivityItem | null {
  const type = firstString(event.type, event.event_type).toLowerCase()
  const progress = safeProgressText(event)
  if (type === 'mcp_catalog_loading') {
    return {
      id: 'mcp-catalog',
      kind: 'event',
      icon: 'generic',
      title: '正在准备 MCP 工具目录',
      detail: safeMcpServerNames(event) || '正在读取缓存或发现可用工具',
      status: 'running',
    }
  }
  if (type === 'mcp_connecting') {
    const servers = safeMcpServerNames(event)
    return {
      id: `mcp-server-${servers || itemIndex}`,
      kind: 'event',
      icon: 'generic',
      title: '正在连接 MCP 服务',
      detail: servers || '正在启动 MCP 服务',
      status: 'running',
    }
  }
  if (type === 'mcp_server_ready') {
    return {
      id: `mcp-server-${firstString(event.server) || itemIndex}`,
      kind: 'event',
      icon: 'generic',
      title: 'MCP 服务已按需连接',
      detail: firstString(event.server) || '连接已经就绪',
      status: 'completed',
    }
  }
  if (type === 'mcp_ready') {
    const toolCount = mcpToolCount(event)
    return {
      id: 'mcp-catalog',
      kind: 'event',
      icon: 'generic',
      title: 'MCP 已就绪',
      detail: toolCount ? `已发现 ${toolCount} 个工具` : '服务已连接',
      status: 'completed',
    }
  }
  if (type === 'mcp_degraded') {
    const toolCount = mcpToolCount(event)
    return {
      id: 'mcp-connection',
      kind: 'event',
      icon: 'generic',
      title: '部分 MCP 服务不可用',
      detail: toolCount ? `仍可使用 ${toolCount} 个工具` : '本轮将继续使用其他工具',
      status: 'failed',
    }
  }
  if (type === 'model_step_started') {
    return {
      id: thoughtItemId(event, itemIndex),
      kind: 'thought',
      icon: 'think',
      title: '思考',
      // Keep the generic model-step marker available for the live phase, but
      // do not make it count as expandable content unless the runtime emits
      // an explicit safe progress summary.
      detail: progress,
      status: 'running',
    }
  }
  if (type === 'model_retry') {
    const attempt = typeof event.attempt === 'number' ? `第 ${event.attempt} 次重试` : '正在重试模型'
    return { id: firstString(event.event_id, event.id) || `retry-${itemIndex}`, kind: 'event', icon: 'think', title: '重试模型', detail: progress || attempt, status: 'running' }
  }
  if (type === 'completion_verification_started' || type === 'completion_verification_passed' || type === 'completion_verification_rejected') {
    const passed = type.endsWith('passed')
    const rejected = type.endsWith('rejected')
    const attempt = typeof event.attempt === 'number' ? `第 ${event.attempt} 次` : ''
    return {
      id: firstString(event.event_id, event.id) || `verification-${itemIndex}`,
      kind: 'event',
      icon: passed ? 'approval' : 'think',
      title: passed ? '结果验收通过' : rejected ? '结果验收未通过' : '正在验收结果',
      detail: progress || attempt,
      status: passed ? 'completed' : rejected ? 'failed' : 'running',
    }
  }
  if (contextActivityTypes.has(type)) {
    const title = type.includes('compact') ? '整理上下文' : '准备上下文'
    return { id: firstString(event.event_id, event.id) || `context-${itemIndex}`, kind: 'context', icon: 'context', title, detail: progress || '正在整理本轮需要的信息', status: 'running' }
  }
  if (type === 'approval_requested' || type === 'approval_granted') {
    const granted = type === 'approval_granted'
    return { id: firstString(event.event_id, event.id, event.approval_id) || `approval-${itemIndex}`, kind: 'approval', icon: 'approval', title: granted ? '审批通过' : '等待审批', detail: progress || (granted ? '继续执行已批准的操作' : '需要你的确认后继续'), status: granted ? 'completed' : 'running' }
  }
  if (type.startsWith('delegated_child_')) {
    const completed = type.endsWith('completed')
    const failed = type.endsWith('failed') || type.endsWith('stopped')
    return { id: firstString(event.event_id, event.id, event.task_id, event.child_run_id) || `task-${itemIndex}`, kind: 'task', icon: 'task', title: completed ? '子 Agent 已返回' : failed ? '子 Agent 已停止' : '子 Agent 工作中', detail: progress || (completed ? '已收到子 Agent 的凝练结果' : failed ? '子 Agent 未完成任务' : '正在处理专长任务'), status: completed ? 'completed' : failed ? 'failed' : 'running' }
  }
  if (safeProgressTypes.has(type) && progress) {
    return { id: firstString(event.event_id, event.id) || `activity-${itemIndex}`, kind: 'event', icon: 'think', title: '进度', detail: progress, status: 'running' }
  }
  return null
}

export function updateThoughtTimeline(
  state: ThoughtTimelineState,
  event: RunStreamEvent,
  now = Date.now(),
): ThoughtTimelineState {
  const type = firstString(event.type, event.event_type).toLowerCase()
  const start = state.startedAt ?? (type === 'model_step_started' || type === 'mcp_connecting' || toolStartTypes.has(type) ? now : null)
  if (type === 'thought_delta' || type === 'thought_summary') {
    const rawText = type === 'thought_delta'
      ? firstString(event.delta)
      : firstString(event.summary, record(event.payload)?.summary)
    const text = cleanThoughtText(rawText)
    if (!text) return start === state.startedAt ? state : { ...state, startedAt: start }
    const items = [...(state.items || [])]
    const id = thoughtItemId(event, items.length)
    let index = items.findIndex((item) => item.id === id)
    if (index < 0) index = items.findLastIndex((item) => item.kind === 'thought' && item.status === 'running')
    const complete = type === 'thought_summary' && event.complete === true
    if (index < 0) {
      items.push({ id, kind: 'thought', icon: 'think', title: '思考', detail: text, status: complete ? 'completed' : 'running' })
    } else {
      const current = items[index]
      items[index] = {
        ...current,
        detail: type === 'thought_delta' ? cleanThoughtText(current.detail + text) : text,
        status: complete ? 'completed' : current.status,
      }
    }
    const resolvedId = index < 0 ? id : items[index].id
    return {
      ...state,
      startedAt: start ?? now,
      items,
      activeItemId: complete ? (state.activeItemId === resolvedId ? undefined : state.activeItemId) : resolvedId,
    }
  }
  if (toolStartTypes.has(type)) {
    const name = firstString(event.tool_name, event.name, record(event.payload)?.tool_name, record(event.payload)?.name) || 'Tool'
    const id = firstString(event.tool_call_id, event.call_id, event.id, event.event_id) || `${name}-${state.tools.length}`
    if (state.tools.some((tool) => tool.id === id)) return { ...state, startedAt: start }
    const detail = safeActivityDetail(event, name)
    return {
      ...state,
      startedAt: start,
      tools: [...state.tools, { id, name: displayToolName(name), target: safeToolTarget(event), status: 'running' }],
      items: [...(state.items || []), { id, kind: 'tool', icon: thoughtIconForTool(name), title: displayToolName(name), detail, status: 'running' }],
      activeItemId: id,
    }
  }

  if (toolFinishTypes.has(type)) {
    const id = firstString(event.tool_call_id, event.call_id, event.id, event.event_id)
    const rawName = firstString(event.tool_name, event.name, record(event.payload)?.tool_name, record(event.payload)?.name)
    const displayName = rawName ? displayToolName(rawName) : ''
    let matchIndex = id ? state.tools.findIndex((tool) => tool.id === id) : -1
    if (matchIndex < 0 && displayName) matchIndex = state.tools.findLastIndex((tool) => tool.name === displayName && tool.status === 'running')
    if (matchIndex < 0) matchIndex = state.tools.findLastIndex((tool) => tool.status === 'running')
    if (matchIndex < 0) return state
    const failed = Boolean(event.error) || (event.ok === false && event.pending_approval !== true)
    const tools = state.tools.map((tool, index) => index === matchIndex
      ? { ...tool, status: failed ? 'failed' as const : 'completed' as const }
      : tool)
    const matchedId = state.tools[matchIndex]?.id
    const items = (state.items || []).map((item) => item.id === matchedId
      ? { ...item, status: failed ? 'failed' as const : 'completed' as const, detail: safeActivityDetail(event, rawName) || item.detail }
      : item)
    return { ...state, tools, items, activeItemId: state.activeItemId === matchedId ? undefined : state.activeItemId }
  }

  let activity = activityFromNonToolEvent(event, (state.items || []).length)
  if (activity?.kind === 'task') {
    activity = { ...activity, id: stableDelegationActivityId(event, activity.id) }
  }
  if (activity?.kind === 'event' && type.startsWith('completion_verification_')) {
    const reason = verificationReason(event, type)
    if (reason) activity = { ...activity, detail: reason }
  }
  if (activity) {
    const items = [...(state.items || [])]
    const existingIndex = items.findIndex((item) => item.id === activity.id)
    if (existingIndex >= 0) {
      const existing = items[existingIndex]
      items[existingIndex] = {
        ...existing,
        ...activity,
        detail: activity.detail || existing.detail,
      }
    } else {
      // A new model step closes the previous progress marker while tool
      // entries remain independently tracked below it.
      if (activity.kind === 'thought') {
        for (let index = items.length - 1; index >= 0; index -= 1) {
          if (items[index].kind === 'thought' && items[index].status === 'running') {
            items[index] = { ...items[index], status: 'completed' }
            break
          }
        }
      }
      items.push(activity)
    }
    return {
      ...state,
      startedAt: start,
      items,
      activeItemId: activity.status === 'running' ? activity.id : (state.activeItemId === activity.id ? undefined : state.activeItemId),
    }
  }

  if (terminalTypes.has(type) || (type === 'run_state' && (event.terminal === true || terminalStatuses.has(String(event.status || ''))))) {
    const conclusion = safeProgressText(event) || state.conclusion
    const partialThought = terminalTypes.has(type) ? cleanThoughtText(firstString(event.partial_thought)) : ''
    const terminalFailed = Boolean(event.error)
      || type === 'model_failed'
      || type === 'integration_failed'
      || type === 'failed'
      || String(event.status || '') === 'failed'
    const items = (state.items || []).map((item) => item.status === 'running' ? { ...item, status: terminalFailed ? 'failed' as const : 'completed' as const } : item)
    if (partialThought) {
      const thoughtIndex = items.findLastIndex((item) => item.kind === 'thought')
      if (thoughtIndex >= 0) items[thoughtIndex] = { ...items[thoughtIndex], detail: partialThought }
      else items.push({ id: `thought-interrupted-${items.length}`, kind: 'thought', icon: 'think', title: '思考', detail: partialThought, status: 'completed' })
    }
    return {
      ...state,
      startedAt: start,
      elapsedMs: start === null ? state.elapsedMs : Math.max(state.elapsedMs, now - start),
      finished: true,
      tools: state.tools.map((tool) => tool.status === 'running' ? { ...tool, status: terminalFailed ? 'failed' : 'completed' } : tool),
      items,
      activeItemId: undefined,
      conclusion,
    }
  }

  return start === state.startedAt ? state : { ...state, startedAt: start }
}

export function timelineFromRunEvents(events: RunStreamEvent[]): ThoughtTimelineState {
  let timeline = emptyThoughtTimeline
  for (const event of events) {
    const payload = record(event.payload) ?? {}
    const type = firstString(event.type, event.event_type, payload.type, payload.event_type)
    if (!type) continue
    const flattened = { ...payload, ...event, type } as RunStreamEvent
    const createdAt = firstString(event.created_at, event.timestamp)
    const parsedTime = createdAt ? Date.parse(createdAt) : Number.NaN
    const fallbackElapsed = typeof payload.elapsed_ms === 'number' ? payload.elapsed_ms : typeof event.elapsed_ms === 'number' ? event.elapsed_ms : 0
    const eventTime = Number.isFinite(parsedTime)
      ? parsedTime
      : timeline.startedAt === null ? fallbackElapsed : timeline.startedAt + fallbackElapsed
    timeline = updateThoughtTimeline(timeline, flattened, eventTime)
    if (timeline.finished && fallbackElapsed > timeline.elapsedMs) timeline = { ...timeline, elapsedMs: fallbackElapsed }
  }
  return timeline
}

export function hasVisibleCompletedThought(timeline: ThoughtTimelineState): boolean {
  return timeline.finished && (timeline.startedAt !== null || timeline.tools.length > 0 || (timeline.items || []).length > 0)
}

export function summarizeThoughtConclusion(content: string, maxLength = 72): string {
  const firstMeaningfulLine = content
    .replace(/```[\s\S]*?```/g, '代码内容已生成')
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s{0,3}(?:#{1,6}|[-*+]\s+|\d+[.)]\s+)/, '').replace(/[*_`]/g, '').trim())
    .find(Boolean)

  if (!firstMeaningfulLine) return '任务已完成'
  if (firstMeaningfulLine.length <= maxLength) return firstMeaningfulLine
  return `${firstMeaningfulLine.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`
}

export function formatThoughtDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.max(0, Math.round(milliseconds))}ms`
  const seconds = milliseconds / 1000
  return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`
}

export function formatLiveThinkingDuration(milliseconds: number): string {
  return `${(Math.max(0, milliseconds) / 1000).toFixed(1)} 秒`
}
