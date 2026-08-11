import type { RunStreamEvent } from './sessionStream'

export interface ThoughtToolItem {
  id: string
  name: string
  target: string
  status: 'running' | 'completed' | 'failed'
}

export interface ThoughtTimelineState {
  startedAt: number | null
  elapsedMs: number
  finished: boolean
  tools: ThoughtToolItem[]
}

export const emptyThoughtTimeline: ThoughtTimelineState = {
  startedAt: null,
  elapsedMs: 0,
  finished: false,
  tools: [],
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
const terminalTypes = new Set(['run_completed', 'completed', 'run_stopped', 'stopped', 'model_failed', 'integration_failed', 'failed'])

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

export function updateThoughtTimeline(
  state: ThoughtTimelineState,
  event: RunStreamEvent,
  now = Date.now(),
): ThoughtTimelineState {
  const start = state.startedAt ?? (event.type === 'model_step_started' || toolStartTypes.has(event.type) ? now : null)
  if (toolStartTypes.has(event.type)) {
    const name = firstString(event.tool_name, event.name, record(event.payload)?.tool_name, record(event.payload)?.name) || 'Tool'
    const id = firstString(event.tool_call_id, event.call_id, event.id, event.event_id) || `${name}-${state.tools.length}`
    if (state.tools.some((tool) => tool.id === id)) return { ...state, startedAt: start }
    return {
      ...state,
      startedAt: start,
      tools: [...state.tools, { id, name: displayToolName(name), target: safeToolTarget(event), status: 'running' }],
    }
  }

  if (toolFinishTypes.has(event.type)) {
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
    return { ...state, tools }
  }

  if (terminalTypes.has(event.type) || (event.type === 'run_state' && event.terminal === true)) {
    return {
      ...state,
      startedAt: start,
      elapsedMs: start === null ? state.elapsedMs : Math.max(state.elapsedMs, now - start),
      finished: true,
      tools: state.tools.map((tool) => tool.status === 'running' ? { ...tool, status: event.error ? 'failed' : 'completed' } : tool),
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
  return timeline.finished && (timeline.startedAt !== null || timeline.tools.length > 0)
}

export function formatThoughtDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.max(0, Math.round(milliseconds))}ms`
  const seconds = milliseconds / 1000
  return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`
}

export function formatLiveThinkingDuration(milliseconds: number): string {
  return `${(Math.max(0, milliseconds) / 1000).toFixed(1)} 秒`
}
