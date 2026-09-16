// 本测试文件验证 api 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, describeError } from './api'
import type { MemorySettings, RunEventFilters } from './types'

afterEach(() => {
  vi.unstubAllGlobals()
})

// 测试分组：API 客户端。
describe('API 客户端', () => {
  // 测试场景：可以从常见的命名字段中解包列表。
  it('可以从常见的命名字段中解包列表', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ workspaces: [{ id: 'ws-1', name: '示例工作区' }] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))

    const result = await api.list<{ id: string; name: string }>('/api/workspaces', ['workspaces'])
    expect(result).toEqual([{ id: 'ws-1', name: '示例工作区' }])
  })

  // 测试场景：后端不可连接时返回明确的本地服务错误。
  it('后端不可连接时返回明确的本地服务错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    await expect(api.get('/api/health')).rejects.toMatchObject({
      name: 'ApiError',
      status: 0,
      message: '无法连接 PGAgent 后端，请确认本地服务已经启动。',
    })
  })

  // 测试场景：保留后端返回的业务错误详情。
  it('保留后端返回的业务错误详情', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'API Key 无效' }),
      { status: 401, headers: { 'Content-Type': 'application/json' } },
    )))

    try {
      await api.post('/api/connections', {})
      throw new Error('请求本应失败')
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError)
      expect(describeError(error)).toBe('API Key 无效')
    }
  })

  // 测试场景：会话模型可以通过 PATCH 切换或恢复自动继承。
  it('会话模型可以通过 PATCH 切换或恢复自动继承', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ id: 'session-1', model_id: null }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await api.patch('/api/sessions/session-1', { model_connection_id: null, model_id: null })

    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1', expect.objectContaining({
      method: 'PATCH',
      body: JSON.stringify({ model_connection_id: null, model_id: null }),
    }))
  })

  // 测试场景：可以读取和更新全局记忆开关。
  it('可以读取和更新全局记忆开关', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ enabled: false }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ enabled: true }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ))
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.get<MemorySettings>('/api/memories/settings')).resolves.toEqual({ enabled: false })
    await expect(api.put<MemorySettings>('/api/memories/settings', { enabled: true })).resolves.toEqual({ enabled: true })

    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/memories/settings', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ enabled: true }),
    }))
  })

  // 测试场景：可以通过 DELETE 永久删除一条会话。
  it('可以通过 DELETE 永久删除一条会话', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await api.delete('/api/sessions/session-1')

    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1', expect.objectContaining({
      method: 'DELETE',
    }))
  })

  // 测试场景：原生文件夹选择使用无路径参数的本地 POST 接口。
  it('原生文件夹选择使用无路径参数的本地 POST 接口', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ path: 'C:\\Projects\\PGAgent' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await api.post('/api/system/select-folder')

    expect(fetchMock).toHaveBeenCalledWith('/api/system/select-folder', expect.objectContaining({ method: 'POST' }))
  })

  // 测试场景：multipart 请求交给浏览器生成 Content-Type boundary。
  it('multipart 请求交给浏览器生成 Content-Type boundary', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ id: 'run-1' }),
      { status: 202, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)
    const form = new FormData()
    form.append('payload', JSON.stringify({ content: '分析附件' }))
    form.append('files', new File(['hello'], 'hello.txt', { type: 'text/plain' }))

    await api.postForm('/api/sessions/session-1/turns', form)

    const options = fetchMock.mock.calls[0][1] as RequestInit
    expect(options.body).toBe(form)
    expect(new Headers(options.headers).has('Content-Type')).toBe(false)
  })

  // 测试场景：运行中的会话配置冲突会保留后端 409 提示。
  it('运行中的会话配置冲突会保留后端 409 提示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: '运行或审批期间不能更改模型配置' }),
      { status: 409, headers: { 'Content-Type': 'application/json' } },
    )))

    await expect(api.patch('/api/sessions/session-1', { thinking_level: 'high' })).rejects.toMatchObject({
      status: 409,
      message: '运行或审批期间不能更改模型配置',
    })
  })

  // 测试场景：运行事件请求完整传递诊断筛选与游标，并保留分页响应。
  it('运行事件请求完整传递诊断筛选与游标', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({
        items: [{ id: 'event-42', run_id: 'run/42', event_type: 'model_retry', sequence: 42, step: 3, payload: {}, created_at: '2026-09-08T08:00:00Z' }],
        next_before: 42,
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)
    const filters = {
      event_type: 'model_retry',
      step: 3,
      errors_only: true,
      before: 64,
      limit: 25,
    } satisfies RunEventFilters

    await expect(api.listRunEvents('run/42', filters)).resolves.toMatchObject({
      items: [expect.objectContaining({ id: 'event-42', sequence: 42 })],
      next_before: 42,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/runs/run%2F42/events?event_type=model_retry&step=3&errors_only=true&before=64&limit=25',
      expect.objectContaining({ headers: expect.any(Object) }),
    )
  })

  it('工具结果分页请求会编码运行与调用标识', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({
        tool_call_id: 'call/42', tool_name: 'shell', ok: true, content: 'page',
        offset: 24_000, next_offset: 24_004, total_chars: 24_004, eof: true,
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.getToolResultPage('run/42', 'call/42', 24_000)).resolves.toMatchObject({
      content: 'page', next_offset: 24_004, eof: true,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/runs/run%2F42/tool-results/call%2F42?offset=24000&limit=24000',
      expect.objectContaining({ headers: expect.any(Object) }),
    )
  })
})
