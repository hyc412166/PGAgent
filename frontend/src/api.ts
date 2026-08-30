import type { ApiRecord } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? ''

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

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

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response
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

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  postForm: <T>(path: string, body: FormData) => request<T>(path, { method: 'POST', body }),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  list: async <T>(path: string, keys: string[] = []) => asList<T>(await request<unknown>(path), keys),
}

export function describeError(error: unknown): string {
  return error instanceof Error ? error.message : '发生未知错误，请稍后重试。'
}
