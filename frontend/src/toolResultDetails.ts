import { api } from './api'
import type { ApiRecord, ToolResultPage } from './types'

export type CompleteToolResult = {
  toolCallId: string
  toolName: string
  ok: boolean
  content: string
  totalChars: number
}

export type PresentedToolResult = {
  content: string
  status: Array<[string, string]>
}

export type ToolResultPageFetcher = (
  runId: string,
  toolCallId: string,
  offset: number,
  limit?: number,
) => Promise<ToolResultPage>

export function toolResultRequest(runId: string | undefined, live: boolean, itemKind: string, itemId: string) {
  return !live && runId && itemKind === 'tool' ? { runId, toolCallId: itemId } : null
}

const PAGE_SIZE = 24_000
const visibleMetadataKeys = [
  'artifact_id', 'offset', 'next_offset', 'total_chars', 'eof',
  'command', 'exit_code', 'truncated', 'output_truncated',
  'attachment_id', 'extracted_chars',
] as const

function formatJsonValue(value: unknown): string {
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2)
    } catch {
      return value
    }
  }
  return JSON.stringify(value, null, 2)
}

function displayMetadataValue(value: unknown): string {
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (value === null) return 'null'
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

function recordValue(value: unknown): ApiRecord | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as ApiRecord : undefined
}

function parseJsonString(value: unknown): unknown {
  if (typeof value !== 'string') return value
  try {
    return JSON.parse(value)
  } catch {
    return value
  }
}

function safeVisibleValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(safeVisibleValue)
  const record = recordValue(value)
  if (!record) return value
  return Object.fromEntries(Object.entries(record)
    .filter(([key]) => !['storage_path', 'provider_payload'].includes(key))
    .map(([key, child]) => [key, safeVisibleValue(child)]))
}

// 只从 ToolResult 外层提取用户可见正文和已批准的运行状态，不暴露存储与 provider 内部信息。
export function presentToolResult(persistedContent: string): PresentedToolResult {
  let parsed: unknown
  try {
    parsed = JSON.parse(persistedContent)
  } catch {
    return { content: persistedContent, status: [] }
  }
  if (!recordValue(parsed)) {
    return { content: formatJsonValue(parsed), status: [] }
  }
  const status: Array<[string, string]> = []
  const seenStatus = new Set<string>()
  let current: unknown = parsed
  let wrapper = recordValue(current)
  let unwrapped = false
  let finalToolName = ''
  // 只有 ToolResult 契约同时存在 tool_name 和 content 时才继续解包，避免误拆普通业务 JSON。
  while (wrapper && typeof wrapper.tool_name === 'string' && Object.prototype.hasOwnProperty.call(wrapper, 'content')) {
    unwrapped = true
    finalToolName = wrapper.tool_name
    const metadata = recordValue(wrapper.metadata)
    for (const key of visibleMetadataKeys) {
      if (metadata && !seenStatus.has(key) && Object.prototype.hasOwnProperty.call(metadata, key)) {
        status.push([key, displayMetadataValue(metadata[key])])
        seenStatus.add(key)
      }
    }
    current = parseJsonString(wrapper.content)
    wrapper = recordValue(current)
  }
  const finalPayload = recordValue(current)
  if (finalPayload && ['shell', 'bash', 'run_command'].includes(finalToolName)) {
    const output = typeof finalPayload.output === 'string' ? finalPayload.output : ''
    const error = typeof finalPayload.error === 'string' ? finalPayload.error : ''
    const backgroundPayload = ['id', 'session_id', 'run_id', 'log_path', 'pid'].some((key) => Object.prototype.hasOwnProperty.call(finalPayload, key))
      || (
        Object.prototype.hasOwnProperty.call(finalPayload, 'command')
        && Object.prototype.hasOwnProperty.call(finalPayload, 'shell')
        && Object.prototype.hasOwnProperty.call(finalPayload, 'exit_code')
        && (Object.prototype.hasOwnProperty.call(finalPayload, 'output') || Object.prototype.hasOwnProperty.call(finalPayload, 'error'))
      )
    const shellContent = error && output ? `${output}\n\n错误：${error}` : output || error || (backgroundPayload ? '无输出' : formatJsonValue(safeVisibleValue(finalPayload)))
    for (const key of ['command', 'exit_code', 'truncated', 'output_truncated'] as const) {
      if (!seenStatus.has(key) && Object.prototype.hasOwnProperty.call(finalPayload, key)) {
        status.push([key, displayMetadataValue(finalPayload[key])])
        seenStatus.add(key)
      }
    }
    return { content: shellContent, status }
  }
  const safeOuter = Object.fromEntries(Object.entries(parsed as ApiRecord)
    .filter(([key]) => !['storage_path', 'provider_payload', 'metadata'].includes(key)))
  return {
    content: unwrapped
      ? formatJsonValue(safeVisibleValue(current))
      : JSON.stringify(safeVisibleValue(safeOuter), null, 2),
    status,
  }
}

export async function loadCompleteToolResult(
  runId: string,
  toolCallId: string,
  fetchPage: ToolResultPageFetcher = api.getToolResultPage,
): Promise<CompleteToolResult> {
  let offset = 0
  let content = ''
  let page: ToolResultPage
  do {
    page = await fetchPage(runId, toolCallId, offset, PAGE_SIZE)
    content += page.content
    if (!page.eof && page.next_offset <= offset) {
      throw new Error('工具结果分页游标未前进，无法继续读取。')
    }
    offset = page.next_offset
  } while (!page.eof)
  return {
    toolCallId: page.tool_call_id,
    toolName: page.tool_name,
    ok: page.ok,
    content,
    totalChars: page.total_chars,
  }
}

export function createToolResultDetailLoader(fetchPage: ToolResultPageFetcher = api.getToolResultPage) {
  const cache = new Map<string, Promise<CompleteToolResult>>()
  return {
    load(runId: string, toolCallId: string) {
      const key = `${runId}:${toolCallId}`
      const cached = cache.get(key)
      if (cached) return cached
      const request = loadCompleteToolResult(runId, toolCallId, fetchPage)
      cache.set(key, request)
      request.catch(() => cache.delete(key))
      return request
    },
  }
}
