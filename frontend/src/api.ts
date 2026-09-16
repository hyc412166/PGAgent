// 本文件负责 api 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { ApiRecord, RunEventFilters, RunEventPage, ToolResultPage } from './types'

// API_BASE 是后端地址前缀；删除末尾斜杠，避免与各接口路径拼接出双斜杠。
const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? ''

// 将相对接口路径转换为 fetch、EventSource 等浏览器 API 可直接使用的完整地址。
export function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

// ApiError 统一保存 HTTP 状态码和后端原始错误体，页面只展示 message，诊断时仍可读取 details。
export class ApiError extends Error {
  status: number
  details?: unknown

  constructor(message: string, status: number, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.details = details
  }
}

// 从后端几种兼容错误格式中提取用户可读消息；无法识别时退回 HTTP 状态说明。
function errorMessage(body: unknown, status: number): string {
  if (body && typeof body === 'object') {
    const record = body as ApiRecord
    const detail = record.detail ?? record.message ?? record.error
    if (typeof detail === 'string' && detail.trim()) return detail
    if (Array.isArray(detail)) return detail.map((item) => JSON.stringify(item)).join('；')
    if (detail && typeof detail === 'object') {
      const nested = detail as ApiRecord
      if (typeof nested.message === 'string') return nested.message
    }
  }
  return `请求失败（HTTP ${status}）`
}

// 所有 REST 请求的唯一底层入口：补齐公共请求头、解析响应并把网络或业务失败转换为 ApiError。
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response
  // formDataBody 用于保留浏览器自动生成的 multipart boundary，不能手动写 JSON Content-Type。
  const formDataBody = typeof FormData !== 'undefined' && options.body instanceof FormData
  try {
    response = await fetch(apiUrl(path), {
      ...options,
      headers: {
        Accept: 'application/json',
        ...(options.body && !formDataBody ? { 'Content-Type': 'application/json' } : {}),
        ...options.headers,
      },
    })
  } catch (error) {
    throw new ApiError('无法连接 PGAgent 后端，请确认本地服务已经启动。', 0, error)
  }

  const contentType = response.headers.get('content-type') ?? ''
  const body: unknown = contentType.includes('application/json')
    ? await response.json().catch(() => null)
    : await response.text().catch(() => '')
  if (!response.ok) throw new ApiError(errorMessage(body, response.status), response.status, body)
  return body as T
}

// 兼容后端直接数组和常见包裹结构，将列表接口结果规范为统一数组。
function asList<T>(value: unknown, keys: string[] = []): T[] {
  if (Array.isArray(value)) return value as T[]
  if (value && typeof value === 'object') {
    const record = value as ApiRecord
    for (const key of [...keys, 'items', 'data', 'results']) {
      if (Array.isArray(record[key])) return record[key] as T[]
    }
  }
  return []
}

// api 是页面层使用的轻量请求门面；list 额外执行列表结构归一化。
export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  postForm: <T>(path: string, body: FormData) => request<T>(path, { method: 'POST', body }),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  list: async <T>(path: string, keys: string[] = []) => asList<T>(await request<unknown>(path), keys),
  // 运行事件接口保留分页对象，供详情面板使用 next_before 继续读取历史事件。
  listRunEvents: (runId: string, filters: RunEventFilters = {}) => {
    const query = new URLSearchParams()
    if (filters.event_type?.trim()) query.set('event_type', filters.event_type.trim())
    if (filters.step !== undefined) query.set('step', String(filters.step))
    if (filters.errors_only !== undefined) query.set('errors_only', String(filters.errors_only))
    if (filters.before !== undefined) query.set('before', String(filters.before))
    if (filters.limit !== undefined) query.set('limit', String(filters.limit))
    const suffix = query.size ? `?${query.toString()}` : ''
    return request<RunEventPage>(`/api/runs/${encodeURIComponent(runId)}/events${suffix}`)
  },
  getToolResultPage: (runId: string, toolCallId: string, offset = 0, limit = 24_000) =>
    request<ToolResultPage>(`/api/runs/${encodeURIComponent(runId)}/tool-results/${encodeURIComponent(toolCallId)}?offset=${offset}&limit=${limit}`),
}

// 把未知捕获值安全转换为可展示文案，避免页面直接渲染对象。
export function describeError(error: unknown): string {
  return error instanceof Error ? error.message : '发生未知错误，请稍后重试。'
}
