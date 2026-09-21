// 本文件负责 thoughtTimeline 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { RunStreamEvent } from './sessionStream'
import type { FileChangeRecord, FileChangeSet } from './types'

// ThoughtToolItem 表示思考时间线中一次工具调用的开始、结束和结果摘要。
export interface ThoughtToolItem {
  id: string
  name: string
  target: string
  status: 'running' | 'completed' | 'failed'
}

/**
 * A user-facing processing item. Tool metadata is sanitized separately;
 * Only model-authored, user-visible progress is shown here. Provider
 * reasoning summaries remain outside the presentation timeline.
 */
// ActivityKind/Icon 分别表达活动语义和对应的视觉图标类别。
export type ThoughtActivityKind = 'thought' | 'tool' | 'file-change' | 'context' | 'approval' | 'task' | 'event' | 'assistant'
export type ThoughtActivityIcon = 'think' | 'read' | 'write' | 'edit' | 'search' | 'shell' | 'task' | 'approval' | 'context' | 'generic'

// ThoughtActivityItem 是用户可见的单条推理活动摘要。
export interface ThoughtActivityItem {
  id: string
  kind: ThoughtActivityKind
  icon: ThoughtActivityIcon
  title: string
  detail: string
  status: 'running' | 'completed' | 'failed'
  // assistant 条目承载模型明确标记为 commentary/final_answer 的可见正文。
  phase?: 'commentary' | 'final_answer' | 'unknown' | string
  changeSet?: FileChangeSet
}

// ThoughtTimelineState 聚合当前运行的思考文本、活动、工具项和计时边界。
export interface ThoughtTimelineState {
  startedAt: number | null
  elapsedMs: number
  finished: boolean
  tools: ThoughtToolItem[]
  items: ThoughtActivityItem[]
  activeItemId?: string
  conclusion?: string
}

// emptyThoughtTimeline 是每轮运行开始时的不可变初始快照。
export const emptyThoughtTimeline: ThoughtTimelineState = {
  startedAt: null,
  elapsedMs: 0,
  finished: false,
  tools: [],
  items: [],
  activeItemId: undefined,
}

// thinkingPhrases/faces 为等待模型首个可见事件时提供稳定但友好的状态占位。
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

// 使用可注入随机源选择状态，便于测试覆盖边界。
export function pickThinkingStatus(random: () => number = Math.random): string {
  const phraseIndex = Math.min(thinkingPhrases.length - 1, Math.max(0, Math.floor(random() * thinkingPhrases.length)))
  const faceIndex = Math.min(thinkingFaces.length - 1, Math.max(0, Math.floor(random() * thinkingFaces.length)))
  return `${thinkingPhrases[phraseIndex]} ${thinkingFaces[faceIndex]}`
}

// 根据 runId 稳定选择短语，使同一运行重渲染时文案不跳变。
export function thinkingStatusForRun(runId: string): string {
  let hash = 2166136261
  for (const character of runId || 'pending') hash = Math.imul(hash ^ character.charCodeAt(0), 16777619)
  let call = 0
  return pickThinkingStatus(() => {
    const shifted = call++ === 0 ? hash : Math.imul(hash ^ 0x9e3779b9, 16777619)
    return (shifted >>> 0) / 0x1_0000_0000
  })
}

// 事件类型集合用于识别工具生命周期和整轮终态。
const toolStartTypes = new Set(['tool_started', 'tool_call'])
const toolFinishTypes = new Set(['tool_finished', 'tool_result'])
const fileMutationTools = new Set(['apply_patch', 'edit', 'edit_file', 'write', 'write_file', 'delete'])
const assistantEventTypes = new Set(['assistant_message_started', 'assistant_message_delta', 'assistant_message_completed', 'assistant_delta'])
const terminalTypes = new Set(['turn_completed', 'turn_failed', 'turn_stopped', 'run_completed', 'completed', 'run_interrupted', 'run_stopped', 'stopped', 'model_failed', 'integration_failed', 'failed'])
const terminalStatuses = new Set(['completed', 'stopped', 'failed', 'cancelled'])

// 以下安全提取函数从不同版本事件结构中读取展示字段，并控制敏感或超长内容。
function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

function firstString(...values: unknown[]): string {
  return values.find((value) => typeof value === 'string' && value.trim())?.toString().trim() ?? ''
}

// 提取工具最有辨识度的目标（路径、查询、命令等）并限制长度。
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
    // 文件路径按普通文本处理，避免把本地路径误当作可执行链接或富文本。
  }

  if (target.length <= maxLength) return target
  const head = Math.ceil((maxLength - 1) * 0.58)
  const tail = Math.floor((maxLength - 1) * 0.42)
  return `${target.slice(0, head)}…${target.slice(-tail)}`
}

// 将内部工具名映射为用户可理解的动作名称。
export function displayToolName(name: string): string {
  const normalized = name.trim().toLowerCase()
  const labels: Record<string, string> = {
    read: 'Read',
    read_file: 'Read',
    read_artifact: '读取文件',
    write: 'Write',
    write_file: 'Write',
    edit: 'Edit',
    webfetch: 'WebFetch',
    web_fetch: 'WebFetch',
    web_open: 'Web Open',
    websearch: 'WebSearch',
    web_search: 'WebSearch',
    web_run: '联网搜索',
    bash: 'Shell',
    shell: 'Shell',
    run_command: 'Shell',
    apply_patch: 'Apply Patch',
    update_plan: 'Update Plan',
    todowrite: 'Update Plan',
    tool_search: 'Tool Search',
    toolsearch: 'Tool Search',
    rg: 'Search',
    glob: 'Glob',
    git_status: 'Git Status',
    git_diff: 'Git Diff',
  }
  return (labels[normalized] ?? name.trim()) || 'Tool'
}

// 根据工具语义选择读取、写入、搜索、终端或通用图标。
export function thoughtIconForTool(name: string): ThoughtActivityIcon {
  const normalized = name.trim().toLowerCase()
  if (normalized === 'task' || normalized === 'update_plan' || normalized === 'todowrite' || normalized.includes('delegate')) return 'task'
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

export function parseFileChangeSet(value: unknown): FileChangeSet | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const candidate = value as Record<string, unknown>
  if (!Array.isArray(candidate.files)) return undefined
  const files = candidate.files.flatMap((value): FileChangeRecord[] => {
    const item = record(value)
    const path = typeof item?.path === 'string' ? item.path.trim() : ''
    if (!path) return []
    const operation = item?.operation === 'add' || item?.operation === 'delete' ? item.operation : 'update'
    const change: FileChangeRecord = { path, operation }
    for (const key of ['added_lines', 'deleted_lines', 'line_count', 'first_changed_line'] as const) {
      const raw = item?.[key]
      if (typeof raw === 'number' && Number.isFinite(raw) && raw >= 0) change[key] = Math.floor(raw)
      else if ((key === 'line_count' || key === 'first_changed_line') && raw === null) change[key] = null
    }
    if (typeof item?.diff === 'string') change.diff = item.diff
    if (typeof item?.diff_truncated === 'boolean') change.diff_truncated = item.diff_truncated
    if (typeof item?.binary === 'boolean') change.binary = item.binary
    return [change]
  })
  if (!files.length) return undefined
  return {
    status: typeof candidate.status === 'string' ? candidate.status : undefined,
    source: typeof candidate.source === 'string' ? candidate.source : undefined,
    file_count: typeof candidate.file_count === 'number' && Number.isFinite(candidate.file_count)
      ? Math.max(0, Math.floor(candidate.file_count))
      : files.length,
    added_lines: typeof candidate.added_lines === 'number' && Number.isFinite(candidate.added_lines) ? Math.max(0, Math.floor(candidate.added_lines)) : undefined,
    deleted_lines: typeof candidate.deleted_lines === 'number' && Number.isFinite(candidate.deleted_lines) ? Math.max(0, Math.floor(candidate.deleted_lines)) : undefined,
    files,
  }
}

function fileChangeSet(event: RunStreamEvent): FileChangeSet | undefined {
  const payload = record(event.payload)
  return parseFileChangeSet(event.change_set ?? payload?.change_set)
}

function isFileMutationTool(name: string): boolean {
  return fileMutationTools.has(name.trim().toLowerCase())
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

function assistantItemIdentity(event: RunStreamEvent, fallbackIndex: number) {
  const payload = record(event.payload)
  const responseId = firstString(event.response_id, payload?.response_id)
  const itemId = firstString(event.item_id, payload?.item_id)
  const outputIndexValue = event.output_index ?? payload?.output_index
  const outputIndex = typeof outputIndexValue === 'number' && Number.isInteger(outputIndexValue) && outputIndexValue >= 0
    ? outputIndexValue
    : undefined
  const identity = itemId || (outputIndex === undefined ? `legacy-${fallbackIndex}` : `index-${outputIndex}`)
  return {
    id: `${responseId || 'legacy'}:${identity}`,
    responseId: responseId || undefined,
    itemId: itemId || undefined,
    outputIndex,
  }
}

function assistantItemMatches(item: ThoughtActivityItem, identity: ReturnType<typeof assistantItemIdentity>) {
  if (item.kind !== 'assistant') return false
  if (item.id === identity.id) return true
  const sameResponse = !identity.responseId || item.id.startsWith(`${identity.responseId}:`)
  // 某些兼容流会先发没有 response_id 的 delta，再在 completed 事件补齐 response_id；item_id 是此时最稳定的身份。
  if (identity.itemId && item.id.endsWith(`:${identity.itemId}`)) return true
  return identity.outputIndex !== undefined
    && item.id.endsWith(`:index-${identity.outputIndex}`)
    && sameResponse
}

function assistantContent(event: RunStreamEvent): string {
  const payload = record(event.payload)
  const value = [event.content, event.delta, payload?.content, payload?.delta]
    .find((candidate) => typeof candidate === 'string' && candidate.length > 0)
  return typeof value === 'string' ? value : ''
}

function assistantPhase(event: RunStreamEvent, type: string): string {
  const payload = record(event.payload)
  const phase = firstString(event.phase, payload?.phase)
  if (phase) return phase
  return type === 'assistant_delta' ? 'final_answer' : 'unknown'
}

function updateAssistantTimelineItem(
  state: ThoughtTimelineState,
  event: RunStreamEvent,
  type: string,
): ThoughtTimelineState {
  const items = [...(state.items || [])]
  const identity = assistantItemIdentity(event, items.length)
  let index = items.findIndex((item) => assistantItemMatches(item, identity))
  if (index < 0 && !identity.responseId && !identity.itemId) {
    index = items.findLastIndex((item) => item.kind === 'assistant' && item.status === 'running')
  }
  const content = assistantContent(event)
  const deltaEvent = type === 'assistant_message_delta' || type === 'assistant_delta'
  const phase = assistantPhase(event, type)
  const status = type === 'assistant_message_completed' ? 'completed' as const : 'running' as const
  if (index < 0) {
    items.push({
      id: identity.id,
      kind: 'assistant',
      icon: 'think',
      title: phase === 'final_answer' ? '最终回复' : '中间回复',
      detail: content,
      phase,
      status,
    })
  } else {
    const current = items[index]
    items[index] = {
      ...current,
      id: identity.responseId || identity.itemId ? identity.id : current.id,
      title: phase === 'final_answer' ? '最终回复' : current.title || '中间回复',
      detail: deltaEvent ? `${current.detail}${content}` : content || current.detail,
      phase: phase || current.phase,
      status: type === 'assistant_message_completed' ? 'completed' : current.status,
    }
  }
  return { ...state, startedAt: state.startedAt, items, activeItemId: undefined }
}

function safeArgumentDetail(event: RunStreamEvent): string {
  const args = record(event.arguments) ?? record(event.input) ?? record(record(event.payload)?.arguments) ?? record(record(event.payload)?.input)
  if (!args) return ''
  const command = record(args.command)
  if (command?.executable) {
    const count = typeof command.argument_count === 'number' ? command.argument_count : undefined
    return count === undefined ? `执行 ${String(command.executable)}` : `执行 ${String(command.executable)}（${count} 个参数）`
  }
  if (typeof command?.text === 'string' && command.text.trim()) return command.text.trim()
  const query = record(args.query)
  if (typeof query?.text === 'string' && query.text.trim()) return `查询：${query.text.trim()}`
  if (query?.chars !== undefined) return `搜索查询（${String(query.chars)} 字符）`
  if (typeof args.pattern === 'string' && args.pattern.trim()) {
    const path = firstString(args.path, args.file_path)
    return path ? `匹配：${args.pattern.trim()} · 路径：${path}` : `匹配：${args.pattern.trim()}`
  }
  if (typeof args.path === 'string' && args.path.trim()) return `读取：${args.path.trim()}`
  if (typeof args.artifact_id === 'string' && args.artifact_id.trim()) return `读取：${args.artifact_id.trim()}`
  if (typeof args.storage_key === 'string' && args.storage_key.trim()) return `读取：${args.storage_key.trim()}`
  const weather = Array.isArray(args.weather) ? args.weather[0] : record(args.weather)
  if (weather) {
    const weatherRecord = record(weather)
    const weatherItems = Array.isArray(weatherRecord?.items) ? weatherRecord.items : []
    const location = firstString(weatherRecord?.location, weatherRecord?.city, weatherItems[0])
    if (location) return `天气：${location}`
  }
  const searchQuery = Array.isArray(args.search_query) ? args.search_query[0] : record(args.search_query)
  if (searchQuery) {
    const searchQueryRecord = record(searchQuery)
    const text = firstString(searchQueryRecord?.q, searchQueryRecord?.query)
    if (text) return `搜索：${text}`
    const item = Array.isArray(searchQueryRecord?.items) ? searchQueryRecord.items[0] : undefined
    if (typeof item === 'string' && item.trim()) return `搜索：${item.trim()}`
  }
  const searchItems = Array.isArray(searchQuery?.items) ? searchQuery.items : []
  if (searchItems.length) {
    const text = firstString(searchItems[0])
    if (text) return `搜索：${text}`
  }
  const open = Array.isArray(args.open) ? args.open[0] : record(args.open)
  if (open) {
    const openRecord = record(open)
    const ref = firstString(openRecord?.url, openRecord?.ref_id)
    if (ref) return `打开：${ref}`
    const item = Array.isArray(openRecord?.items) ? openRecord.items[0] : undefined
    if (typeof item === 'string' && item.trim()) return `打开：${item.trim()}`
  }
  const openItems = Array.isArray(open?.items) ? open.items : []
  if (openItems.length) {
    const text = firstString(openItems[0])
    if (text) return `打开：${text}`
  }
  const task = record(args.task)
  if (typeof task?.text === 'string' && task.text.trim()) return `任务：${task.text.trim()}`
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
  if (normalized === 'read_artifact') return ''
  if (normalized.includes('read') || normalized.includes('list') || normalized.includes('glob')) return '读取项目内容'
  if (normalized.includes('write') || normalized.includes('create') || normalized.includes('edit') || normalized.includes('patch')) return '准备修改项目文件'
  if (normalized.includes('search') || normalized.includes('fetch') || normalized.includes('browse') || normalized.includes('web')) return '查找相关资料'
  if (normalized === 'update_plan' || normalized === 'todowrite') return '更新任务计划'
  if (normalized === 'task' || normalized.includes('delegate')) return '编排专长子 Agent'
  if (normalized.includes('bash') || normalized.includes('command') || normalized.includes('shell') || normalized.includes('exec')) return '准备执行命令'
  return ''
}

const safeProgressTypes = new Set(['agent_progress', 'thought_summary', 'activity_update'])
const contextActivityTypes = new Set(['context_prepared', 'context_resumed', 'context_compacted', 'context_compaction_started', 'context_compaction_finished', 'context_compaction_failed'])

// 将上下文、审批、委派和进度等非工具事件转换为活动条目。
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
      // 保留通用模型步骤供实时阶段显示；只有运行时给出明确的安全进度摘要时才算可展开内容。
      detail: progress,
      status: 'running',
    }
  }
  if (type === 'model_retry') {
    const attempt = typeof event.attempt === 'number' ? `第 ${event.attempt} 次重试` : '正在重试模型'
    return { id: 'model-retry', kind: 'event', icon: 'think', title: '重试模型', detail: progress || attempt, status: 'running' }
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
    const payload = record(event.payload)
    const taskTitle = firstString(event.task_title, payload?.task_title, event.task, payload?.task)
    return { id: firstString(event.event_id, event.id, event.task_id, event.child_run_id) || `task-${itemIndex}`, kind: 'task', icon: 'task', title: completed ? '子 Agent 已返回' : failed ? '子 Agent 已停止' : '子 Agent 工作中', detail: taskTitle || progress || (completed ? '已收到子 Agent 的凝练结果' : failed ? '子 Agent 未完成任务' : '正在处理专长任务'), status: completed ? 'completed' : failed ? 'failed' : 'running' }
  }
  if (safeProgressTypes.has(type) && progress) {
    return { id: firstString(event.event_id, event.id) || `activity-${itemIndex}`, kind: 'event', icon: 'think', title: '进度', detail: progress, status: 'running' }
  }
  return null
}

// 纯函数式吸收一个 SSE 事件，推进思考文本、工具状态、活动列表和终态时间。
export function updateThoughtTimeline(
  state: ThoughtTimelineState,
  event: RunStreamEvent,
  now = Date.now(),
): ThoughtTimelineState {
  const type = firstString(event.type, event.event_type).toLowerCase()
  const start = state.startedAt ?? (type === 'model_step_started' || type === 'mcp_connecting' || toolStartTypes.has(type) || assistantEventTypes.has(type) ? now : null)
  const payload = record(event.payload)
  if (assistantEventTypes.has(type)) {
    return updateAssistantTimelineItem({ ...state, startedAt: start }, event, type)
  }
  if (type === 'thought_delta') {
    const text = cleanThoughtText(firstString(event.delta, payload?.delta))
    if (!text) return start === state.startedAt ? state : { ...state, startedAt: start }
    const items = [...(state.items || [])]
    const id = thoughtItemId(event, items.length)
    const index = items.findIndex((item) => item.id === id)
    if (index >= 0) items[index] = { ...items[index], detail: `${items[index].detail}${text}`, status: 'running' }
    else items.push({ id, kind: 'thought', icon: 'think', title: '思考', detail: text, status: 'running' })
    return { ...state, startedAt: start ?? now, items, activeItemId: id }
  }
  if (type === 'thought_summary' && (event.complete === true || payload?.complete === true)) {
    return start === state.startedAt ? state : { ...state, startedAt: start }
  }
  if (type === 'thought_summary') {
    const rawText = firstString(event.summary, payload?.summary)
    const text = cleanThoughtText(rawText)
    if (!text) return start === state.startedAt ? state : { ...state, startedAt: start }
    const items = [...(state.items || [])]
    const id = thoughtItemId(event, items.length)
    let index = items.findIndex((item) => item.id === id)
    if (index < 0) index = items.findLastIndex((item) => item.kind === 'thought' && item.status === 'running')
    if (index < 0) {
      items.push({ id, kind: 'thought', icon: 'think', title: '思考', detail: text, status: 'running' })
    } else {
      const current = items[index]
      items[index] = {
        ...current,
        detail: text,
      }
    }
    const resolvedId = index < 0 ? id : items[index].id
    return {
      ...state,
      startedAt: start ?? now,
      items,
      activeItemId: resolvedId,
    }
  }
  if (toolStartTypes.has(type)) {
    const name = firstString(event.tool_name, event.name, record(event.payload)?.tool_name, record(event.payload)?.name) || 'Tool'
    if (name.trim().toLowerCase() === 'tool_search' || name.trim().toLowerCase() === 'toolsearch') return { ...state, startedAt: start }
    const id = firstString(event.tool_call_id, event.call_id, event.id, event.event_id) || `${name}-${state.tools.length}`
    if (state.tools.some((tool) => tool.id === id)) return { ...state, startedAt: start }
    const fileMutation = isFileMutationTool(name)
    const target = safeToolTarget(event)
    const detail = fileMutation ? '' : safeActivityDetail(event, name)
    return {
      ...state,
      startedAt: start,
      tools: [...state.tools, { id, name: displayToolName(name), target: safeToolTarget(event), status: 'running' }],
      items: [...(state.items || []), {
        id,
        // 文件工具和 Shell/搜索一样进入普通活动流，完成时只更新这一行。
        kind: 'tool',
        icon: thoughtIconForTool(name),
        title: fileMutation ? `正在编辑${target ? ` ${target}` : '文件'}` : displayToolName(name),
        detail,
        status: 'running',
      }],
      activeItemId: id,
    }
  }

  if (toolFinishTypes.has(type)) {
    const id = firstString(event.tool_call_id, event.call_id, event.id, event.event_id)
    const rawName = firstString(event.tool_name, event.name, record(event.payload)?.tool_name, record(event.payload)?.name)
    if (rawName.trim().toLowerCase() === 'tool_search' || rawName.trim().toLowerCase() === 'toolsearch') return state
    const displayName = rawName ? displayToolName(rawName) : ''
    let matchIndex = id ? state.tools.findIndex((tool) => tool.id === id) : -1
    if (matchIndex < 0 && displayName) matchIndex = state.tools.findLastIndex((tool) => tool.name === displayName && tool.status === 'running')
    if (matchIndex < 0) matchIndex = state.tools.findLastIndex((tool) => tool.status === 'running')
    if (matchIndex < 0) return state
    const failed = Boolean(event.error) || (event.ok === false && event.pending_approval !== true)
    const resultSummary = firstString(event.result_summary, record(event.payload)?.result_summary)
    const changes = fileChangeSet(event)
    const tools = state.tools.map((tool, index) => index === matchIndex
      ? { ...tool, status: failed ? 'failed' as const : 'completed' as const }
      : tool)
    const matchedId = state.tools[matchIndex]?.id
    const matchedItem = (state.items || []).find((item) => item.id === matchedId)
    const fileMutation = isFileMutationTool(rawName)
      || matchedItem?.title.startsWith('正在编辑')
      || matchedItem?.title.startsWith('已编辑')
    let items = (state.items || []).map((item) => {
      if (item.id !== matchedId) return item
      if (changes?.files?.length && fileMutation) {
        const firstFile = changes.files.length === 1 ? changes.files[0] : undefined
        const added = changes.added_lines ?? changes.files.reduce((sum, file) => sum + (file.added_lines || 0), 0)
        const deleted = changes.deleted_lines ?? changes.files.reduce((sum, file) => sum + (file.deleted_lines || 0), 0)
        return {
          ...item,
          kind: 'tool' as const,
          icon: 'edit' as const,
          title: failed
            ? '文件编辑失败'
            : firstFile ? `已编辑 ${firstFile.path}` : `已编辑 ${changes.files.length} 个文件`,
          detail: failed ? resultSummary || '文件未修改' : `+${added} −${deleted}`,
          changeSet: changes,
          status: failed ? 'failed' as const : 'completed' as const,
        }
      }
      if (fileMutation && failed) {
        return {
          ...item,
          title: '文件编辑失败',
          detail: resultSummary || '文件未修改',
          status: 'failed' as const,
        }
      }
      return { ...item, status: failed ? 'failed' as const : 'completed' as const, detail: resultSummary ? `${item.detail}${item.detail ? ' · ' : ''}${resultSummary}` : item.detail }
    })
    if (changes?.files?.length && !fileMutation) {
      items = [...items, ...changes.files.map((file, index) => ({
        id: `${matchedId}:change:${index}`,
        kind: 'tool' as const,
        icon: 'edit' as const,
        title: failed ? '文件编辑失败' : `已编辑 ${file.path}`,
        detail: failed ? resultSummary || '文件未修改' : `+${file.added_lines || 0} −${file.deleted_lines || 0}`,
        changeSet: { ...changes, file_count: 1, files: [file] },
        status: failed ? 'failed' as const : 'completed' as const,
      }))]
    }
    return { ...state, tools, items, activeItemId: state.activeItemId === matchedId ? undefined : state.activeItemId }
  }

  let activity = activityFromNonToolEvent(event, (state.items || []).length)
  if (activity?.kind === 'task') {
    activity = { ...activity, id: stableDelegationActivityId(event, activity.id) }
  }
  if (activity?.kind === 'event' && type.startsWith('completion_verification_')) activity = null
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
      // 新模型步骤结束上一条进度标记，工具条目则继续独立跟踪。
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
    const terminalFailed = Boolean(event.error)
      || type === 'model_failed'
      || type === 'integration_failed'
      || type === 'failed'
      || String(event.status || '') === 'failed'
    const items = (state.items || []).map((item) => item.status === 'running' ? { ...item, status: terminalFailed ? 'failed' as const : 'completed' as const } : item)
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

// 重放持久化事件恢复已完成运行的思考时间线。
export function timelineFromRunEvents(events: RunStreamEvent[]): ThoughtTimelineState {
  let timeline = emptyThoughtTimeline
  // 事件接口按分页方向可能返回倒序；sequence 是同一 Run 内唯一可靠的顺序。
  const orderedEvents = [...events].sort((left, right) => {
    const leftSequence = typeof left.sequence === 'number' ? left.sequence : Number.MAX_SAFE_INTEGER
    const rightSequence = typeof right.sequence === 'number' ? right.sequence : Number.MAX_SAFE_INTEGER
    if (leftSequence !== rightSequence) return leftSequence - rightSequence
    const leftCreatedAt = Date.parse(firstString(left.created_at, left.timestamp))
    const rightCreatedAt = Date.parse(firstString(right.created_at, right.timestamp))
    if (Number.isFinite(leftCreatedAt) && Number.isFinite(rightCreatedAt) && leftCreatedAt !== rightCreatedAt) return leftCreatedAt - rightCreatedAt
    return 0
  })
  for (const event of orderedEvents) {
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

// 判断终态时间线是否含值得在历史消息上方展示的内容。
export function hasVisibleCompletedThought(timeline: ThoughtTimelineState): boolean {
  return timeline.finished && (timeline.startedAt !== null || timeline.tools.length > 0 || (timeline.items || []).length > 0)
}

// 从完整思考内容生成折叠态的一行结论摘要。
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

// 格式化已完成思考耗时。
export function formatThoughtDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.max(0, Math.round(milliseconds))}ms`
  const roundedSeconds = Math.max(0, Math.round(milliseconds / 1000))
  if (roundedSeconds >= 60) {
    const minutes = Math.floor(roundedSeconds / 60)
    return `${minutes}m ${roundedSeconds % 60}s`
  }
  return `${roundedSeconds}s`
}

// 格式化仍在进行中的思考耗时，保留实时感知需要的精度。
export function formatLiveThinkingDuration(milliseconds: number): string {
  return `${Math.floor(Math.max(0, milliseconds) / 1000)} 秒`
}
