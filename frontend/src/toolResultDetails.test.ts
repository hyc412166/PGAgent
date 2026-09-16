import { describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { ToolResultPage } from './types'
import { ToolResultDetailPanel } from './features/sessions/presentation'

describe('历史工具完整结果', () => {
  it('按 next_offset 逐页合并到 eof', async () => {
    const details = await import('./toolResultDetails')
    const fetchPage = vi.fn()
      .mockResolvedValueOnce({ tool_call_id: 'call-1', tool_name: 'shell', ok: true, content: '{"tool_name":"shell",', offset: 0, next_offset: 21, total_chars: 49, eof: false })
      .mockResolvedValueOnce({ tool_call_id: 'call-1', tool_name: 'shell', ok: true, content: '"ok":true,"content":"done"}', offset: 21, next_offset: 49, total_chars: 49, eof: true })

    await expect(details.loadCompleteToolResult('run-1', 'call-1', fetchPage)).resolves.toEqual({
      toolCallId: 'call-1', toolName: 'shell', ok: true,
      content: '{"tool_name":"shell","ok":true,"content":"done"}', totalChars: 49,
    })
    expect(fetchPage.mock.calls.map((call) => call[2])).toEqual([0, 21])
  })

  it('eof 为 false 但分页游标未前进时立即报错并停止请求', async () => {
    const { loadCompleteToolResult } = await import('./toolResultDetails')
    const fetchPage = vi.fn()
      .mockResolvedValueOnce({ tool_call_id: 'call-stuck', tool_name: 'shell', ok: true, content: 'partial', offset: 0, next_offset: 0, total_chars: 99, eof: false })
      .mockRejectedValueOnce(new Error('不应发起第二次请求'))

    await expect(loadCompleteToolResult('run-1', 'call-stuck', fetchPage))
      .rejects.toThrow('工具结果分页游标未前进')
    expect(fetchPage).toHaveBeenCalledTimes(1)
  })

  it('同一详情的并发与折叠再展开复用一次加载', async () => {
    const { createToolResultDetailLoader } = await import('./toolResultDetails')
    let resolvePage!: (value: ToolResultPage) => void
    const fetchPage = vi.fn(() => new Promise<ToolResultPage>((resolve) => { resolvePage = resolve }))
    const loader = createToolResultDetailLoader(fetchPage)

    const first = loader.load('run-1', 'call-1')
    const duplicate = loader.load('run-1', 'call-1')
    expect(fetchPage).toHaveBeenCalledTimes(1)
    resolvePage({ tool_call_id: 'call-1', tool_name: 'shell', ok: true, content: 'full', offset: 0, next_offset: 4, total_chars: 4, eof: true })
    await expect(first).resolves.toMatchObject({ content: 'full' })
    await expect(duplicate).resolves.toMatchObject({ content: 'full' })
    await loader.load('run-1', 'call-1')
    expect(fetchPage).toHaveBeenCalledTimes(1)
  })

  it('加载失败不缓存，重试会发起新请求', async () => {
    const { createToolResultDetailLoader } = await import('./toolResultDetails')
    const fetchPage = vi.fn()
      .mockRejectedValueOnce(new Error('详情读取失败'))
      .mockResolvedValueOnce({ tool_call_id: 'call-2', tool_name: 'read_artifact', ok: true, content: 'full', offset: 0, next_offset: 4, total_chars: 4, eof: true })
    const loader = createToolResultDetailLoader(fetchPage)

    await expect(loader.load('run-1', 'call-2')).rejects.toThrow('详情读取失败')
    await expect(loader.load('run-1', 'call-2')).resolves.toMatchObject({ content: 'full' })
    expect(fetchPage).toHaveBeenCalledTimes(2)
  })

  it('格式化外层与内层 JSON，并只展示指定状态字段', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    const shell = presentToolResult(JSON.stringify({
      tool_name: 'shell', ok: true,
      content: JSON.stringify({ rows: [{ name: 'a.txt' }] }),
      metadata: { command: 'Get-ChildItem', exit_code: 0, truncated: false, output_truncated: true, storage_path: 'secret', provider_payload: { raw: 'secret-provider' } },
    }))
    expect(shell.content).toBe('{\n  "rows": [\n    {\n      "name": "a.txt"\n    }\n  ]\n}')
    expect(shell.status).toEqual([
      ['command', 'Get-ChildItem'], ['exit_code', '0'], ['truncated', '否'], ['output_truncated', '是'],
    ])
    expect(JSON.stringify(shell)).not.toContain('storage_path')
    expect(JSON.stringify(shell)).not.toContain('secret-provider')
  })

  it('将真实 shell 后台任务 payload 收窄为输出、错误和命令状态', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    const backgroundJob = {
      id: 'job-private', session_id: 'session-private', run_id: 'run-private', status: 'failed',
      command: 'npm test', shell: 'powershell', timeout_seconds: 3600, pid: 4812,
      exit_code: 1, log_path: 'C:/private/background-jobs/job.log',
      output: 'FAIL src/example.test.ts', error: 'Process exited with code 1',
      created_at: '2026-09-15T10:00:00Z', observed_by_run_id: 'run-private',
      output_truncated: false, provider_payload: { raw: 'private-provider' },
    }
    const result = presentToolResult(JSON.stringify({
      tool_name: 'shell', ok: false, content: JSON.stringify(backgroundJob),
      metadata: { command: 'npm test', exit_code: 1, truncated: false, output_truncated: false, session_id: 'session-private' },
    }))

    expect(result.content).toBe('FAIL src/example.test.ts\n\n错误：Process exited with code 1')
    expect(result.status).toEqual([
      ['command', 'npm test'], ['exit_code', '1'], ['truncated', '否'], ['output_truncated', '否'],
    ])
    expect(JSON.stringify(result)).not.toContain('job-private')
    expect(JSON.stringify(result)).not.toContain('session-private')
    expect(JSON.stringify(result)).not.toContain('run-private')
    expect(JSON.stringify(result)).not.toContain('log_path')
    expect(JSON.stringify(result)).not.toContain('4812')
    expect(JSON.stringify(result)).not.toContain('private-provider')
  })

  it('空输出 shell 也不回退渲染内部 background payload', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    const result = presentToolResult(JSON.stringify({
      tool_name: 'shell', ok: true,
      content: JSON.stringify({ id: 'job-empty', session_id: 'session-empty', run_id: 'run-empty', log_path: 'C:/private/job.log', pid: 9123, output: '', error: '' }),
      metadata: { command: 'Write-Output nothing', exit_code: 0, truncated: false, output_truncated: false },
    }))

    expect(result.content).toBe('无输出')
    expect(JSON.stringify(result)).not.toContain('job-empty')
    expect(JSON.stringify(result)).not.toContain('session-empty')
    expect(JSON.stringify(result)).not.toContain('run-empty')
    expect(JSON.stringify(result)).not.toContain('log_path')
    expect(JSON.stringify(result)).not.toContain('9123')
  })

  it('后端已投影的空输出 shell 只显示无输出', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    const result = presentToolResult(JSON.stringify({
      tool_name: 'shell', ok: true,
      content: JSON.stringify({ command: 'Write-Output nothing', shell: 'powershell', exit_code: 0, output: '', error: null, output_truncated: false, truncated: false }),
      metadata: { command: 'Write-Output nothing', exit_code: 0, truncated: false, output_truncated: false },
    }))

    expect(result.content).toBe('无输出')
    expect(result.content).not.toContain('powershell')
    expect(result.status).toEqual([
      ['command', 'Write-Output nothing'], ['exit_code', '0'], ['truncated', '否'], ['output_truncated', '否'],
    ])
  })

  it('后端投影的空 shell payload 只显示无输出而不暴露状态 JSON', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    const result = presentToolResult(JSON.stringify({
      tool_name: 'shell', ok: true,
      content: JSON.stringify({ command: 'Write-Output nothing', shell: 'powershell', exit_code: 0, output: '', error: '', output_truncated: false }),
      metadata: { command: 'Write-Output nothing', exit_code: 0, output_truncated: false },
    }))

    expect(result.content).toBe('无输出')
    expect(result.content).not.toContain('Write-Output nothing')
    expect(result.content).not.toContain('powershell')
    expect(result.content).not.toContain('exit_code')
  })

  it('后端投影的 shell payload 有 output 时只显示输出', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    const result = presentToolResult(JSON.stringify({
      tool_name: 'run_command', ok: true,
      content: JSON.stringify({ command: 'Get-ChildItem', shell: 'powershell', exit_code: 0, output: 'a.txt', error: '', output_truncated: false }),
      metadata: { command: 'Get-ChildItem', exit_code: 0, output_truncated: false },
    }))

    expect(result.content).toBe('a.txt')
    expect(result.content).not.toContain('Get-ChildItem')
    expect(result.content).not.toContain('powershell')
    expect(result.content).not.toContain('exit_code')
  })

  it('普通业务 JSON 的 id 保持可见', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    const result = presentToolResult(JSON.stringify({ id: 'document-7', title: '用户文档' }))
    expect(result.content).toContain('document-7')
  })

  it('展示 read_artifact 与 inspect_pdf 的可读状态', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    expect(presentToolResult(JSON.stringify({ tool_name: 'read_artifact', content: '文件正文', metadata: { artifact_id: 'artifact-7', offset: 20, next_offset: 40, total_chars: 88, eof: false } })).status).toEqual([
      ['artifact_id', 'artifact-7'], ['offset', '20'], ['next_offset', '40'], ['total_chars', '88'], ['eof', '否'],
    ])
    expect(presentToolResult(JSON.stringify({ tool_name: 'inspect_pdf', content: '第一页', metadata: { attachment_id: 'attachment-3', extracted_chars: 1200 } })).status).toEqual([
      ['attachment_id', 'attachment-3'], ['extracted_chars', '1200'],
    ])
  })

  it('解包 read_artifact 内的 inspect_pdf ToolResult 至最终 pages JSON，并汇总各层状态', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    const pages = { pages: [{ page: 1, text: '第一页正文' }], content: '业务 JSON 字段不应被单独抽出' }
    const inspectPdf = {
      tool_name: 'inspect_pdf', ok: true, content: JSON.stringify(pages),
      metadata: { attachment_id: 'attachment-3', extracted_chars: 1200, total_chars: 1300, storage_path: 'private-inner' },
      provider_payload: { raw: 'private-provider' },
    }
    const readArtifact = {
      tool_name: 'read_artifact', ok: true, content: JSON.stringify(inspectPdf),
      metadata: { artifact_id: 'artifact-7', offset: 0, next_offset: 1300, total_chars: 1300, eof: true },
    }

    const presented = presentToolResult(JSON.stringify(readArtifact))

    expect(presented.content).toBe('{\n  "pages": [\n    {\n      "page": 1,\n      "text": "第一页正文"\n    }\n  ],\n  "content": "业务 JSON 字段不应被单独抽出"\n}')
    expect(presented.status).toEqual([
      ['artifact_id', 'artifact-7'], ['offset', '0'], ['next_offset', '1300'], ['total_chars', '1300'], ['eof', '是'],
      ['attachment_id', 'attachment-3'], ['extracted_chars', '1200'],
    ])
    expect(JSON.stringify(presented)).not.toContain('private-inner')
    expect(JSON.stringify(presented)).not.toContain('private-provider')
  })

  it('非对象 JSON 或非 JSON 都作为正常完整文本展示', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    expect(presentToolResult('raw complete output')).toEqual({ content: 'raw complete output', status: [] })
    expect(presentToolResult('["one","two"]')).toEqual({ content: '[\n  "one",\n  "two"\n]', status: [] })
  })

  it('外层结果缺少 content 时仍不展示内部存储与 provider 字段', async () => {
    const { presentToolResult } = await import('./toolResultDetails')
    const presented = presentToolResult(JSON.stringify({
      message: '工具未返回正文',
      storage_path: 'C:/private/result.json',
      provider_payload: { token: 'private-token' },
    }))
    expect(presented.content).toContain('工具未返回正文')
    expect(presented.content).not.toContain('storage_path')
    expect(presented.content).not.toContain('provider_payload')
    expect(presented.content).not.toContain('private-token')
  })

  it('只为带 runId 的历史工具生成详情请求', async () => {
    const { toolResultRequest } = await import('./toolResultDetails')
    const item = { id: 'call-9', kind: 'tool' as const, icon: 'shell' as const, title: 'Shell', detail: 'summary', status: 'completed' as const }
    expect(toolResultRequest('run-9', false, item.kind, item.id)).toEqual({ runId: 'run-9', toolCallId: 'call-9' })
    expect(toolResultRequest('run-9', true, item.kind, item.id)).toBeNull()
    expect(toolResultRequest(undefined, false, item.kind, item.id)).toBeNull()
  })

  it('详情面板显式呈现加载、错误重试与完整内容', () => {
    const loading = renderToStaticMarkup(createElement(ToolResultDetailPanel, { state: { status: 'loading' }, fallbackTitle: 'Shell', onRetry: vi.fn() }))
    const failed = renderToStaticMarkup(createElement(ToolResultDetailPanel, { state: { status: 'error', message: '详情读取失败' }, fallbackTitle: 'Shell', onRetry: vi.fn() }))
    const loaded = renderToStaticMarkup(createElement(ToolResultDetailPanel, {
      state: { status: 'loaded', result: { toolCallId: 'call-1', toolName: 'shell', ok: false, content: 'complete output', totalChars: 15 } },
      fallbackTitle: 'Shell', onRetry: vi.fn(),
    }))

    expect(loading).toContain('aria-busy="true"')
    expect(failed).toContain('role="alert"')
    expect(failed).toContain('重试')
    expect(loaded).toContain('完整结果 · 15 字符')
    expect(loaded).toContain('shell')
    expect(loaded).toContain('失败')
    expect(loaded).toContain('complete output')
  })
})
