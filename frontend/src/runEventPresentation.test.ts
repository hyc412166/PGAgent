// 本测试文件验证 runEventPresentation 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'

import { formatRunEventTime, isHiddenRunEvent, presentRunEvent, runDisplayTitle, runSecondaryLabel } from './runEventPresentation'
import type { Run, RunEvent } from './types'

// 测试分组：run record presentation。
describe('run record presentation', () => {
  it('hides internal tool completion events and marks failed artifact reads as failures', () => {
    expect(isHiddenRunEvent({ event_type: 'tool_finished' })).toBe(true)
    expect(isHiddenRunEvent({ event_type: 'tool_result' })).toBe(true)
    expect(isHiddenRunEvent({ event_type: 'tool_started' })).toBe(false)
    const presented = presentRunEvent({ event_type: 'tool_finished', payload: { tool_name: 'read_artifact', error_code: 'artifact_not_found' } })
    expect(presented.title).toBe('read_artifact 调用失败')
    expect(presented.detail).toBe('工具返回了失败结果')
  })
  // 测试场景：uses the conversation title and keeps the run id as secondary metadata。
  it('uses the conversation title and keeps the run id as secondary metadata', () => {
    const run = {
      id: '01bcbd44-aebe-4d41-9da3-2a1782c5a3de',
      session_title: '调用 Playwright 搜索刘亦菲',
      run_kind: 'initial',
      agent_name: 'PGAgent 主控',
    } satisfies Run

    expect(runDisplayTitle(run)).toBe('调用 Playwright 搜索刘亦菲')
    expect(runSecondaryLabel(run)).toBe('初始运行 · PGAgent 主控 · 01bcbd44')
  })

  // 测试场景：工具事件只展示允许的诊断字段，不回显参数或任意 payload。
  it('工具事件只展示允许的诊断字段', () => {
    const event = {
      event_type: 'tool_started',
      step: 2,
      payload: {
        tool_name: 'mcp__playwright__browser_navigate',
        arguments: {
          url: 'https://example.com/?token=secret',
          command: { text: 'echo private-value' },
        },
        duration_ms: 1420,
        arbitrary: { api_key: 'never-show' },
      },
    } satisfies RunEvent

    const presented = presentRunEvent(event)

    expect(presented.title).toBe('开始调用 mcp__playwright__browser_navigate')
    expect(presented.detail).toBe('工具正在执行')
    expect(presented.facts).toEqual([{ label: '耗时', value: '1 秒' }])
    expect(JSON.stringify(presented)).not.toContain('secret')
    expect(JSON.stringify(presented)).not.toContain('private-value')
    expect(JSON.stringify(presented)).not.toContain('api_key')
  })

  // 测试场景：白名单覆盖重试、错误、子运行和后台任务的安全诊断标识。
  it('只翻译重试错误和关联运行的安全诊断字段', () => {
    const presented = presentRunEvent({
      event_type: 'model_retry',
      payload: {
        attempt: 2,
        delay_seconds: 1.5,
        error_code: 'rate_limited',
        error_type: 'provider_error',
        child_run_id: 'child-run-1',
        child_agent_id: 'child-agent-1',
        background_job_id: 'background-1',
        status: 'failed',
      },
    })

    expect(presented.facts).toEqual([
      { label: '重试次数', value: '2' },
      { label: '重试等待', value: '1.5 秒' },
      { label: '错误代码', value: 'rate_limited' },
      { label: '错误类型', value: 'provider_error' },
      { label: '子运行 ID', value: 'child-run-1' },
      { label: '子 Agent ID', value: 'child-agent-1' },
      { label: '后台任务 ID', value: 'background-1' },
    ])
  })

  // 测试场景：shows the initiating message and context statistics for context preparation。
  it('shows the initiating message and context statistics for context preparation', () => {
    const event = {
      event_type: 'context_prepared',
      payload: {
        message_excerpt: '调用 Playwright MCP 打开浏览器并搜索科比…',
        estimated_tokens: 1342,
        omitted_messages: 2,
      },
    } satisfies RunEvent

    const presented = presentRunEvent(event)

    expect(presented.title).toBe('上下文已准备')
    expect(presented.detail).toBe('用户消息：调用 Playwright MCP 打开浏览器并搜索科比…')
    expect(presented.facts).toEqual([])
  })

  // 测试场景：未知事件不把内部类型或 payload 当作用户文案。
  it('未知事件使用安全的通用描述', () => {
    const presented = presentRunEvent({
      event_type: 'provider_internal_packet',
      payload: {
        summary: '内部调试详情',
        reason: 'credential=secret',
        nested: { raw: '不可展示' },
      },
    })

    expect(presented).toEqual({
      title: '运行事件',
      detail: '记录了一项运行状态变化',
      facts: [],
      tone: 'neutral',
    })
  })

  // 测试场景：formats event timestamps down to seconds。
  it('formats event timestamps down to seconds', () => {
    expect(formatRunEventTime('2026-08-29T02:58:46')).toMatch(/02:58:46/)
  })
})
