import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, describeError } from './api'
import type { MemorySettings } from './types'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('API 客户端', () => {
  it('可以从常见的命名字段中解包列表', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ workspaces: [{ id: 'ws-1', name: '示例工作区' }] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))

    const result = await api.list<{ id: string; name: string }>('/api/workspaces', ['workspaces'])
    expect(result).toEqual([{ id: 'ws-1', name: '示例工作区' }])
  })

  it('后端不可连接时返回明确的本地服务错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    await expect(api.get('/api/health')).rejects.toMatchObject({
      name: 'ApiError',
      status: 0,
      message: '无法连接 PGAgent 后端，请确认本地服务已经启动。',
    })
  })

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

  it('可以通过 DELETE 永久删除一条会话', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await api.delete('/api/sessions/session-1')

    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1', expect.objectContaining({
      method: 'DELETE',
    }))
  })

  it('原生文件夹选择使用无路径参数的本地 POST 接口', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ path: 'C:\\Projects\\PGAgent' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await api.post('/api/system/select-folder')

    expect(fetchMock).toHaveBeenCalledWith('/api/system/select-folder', expect.objectContaining({ method: 'POST' }))
  })

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
})
