// 本测试文件验证运行详情的筛选控件与游标分页合并行为。
import { createElement, type ElementType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { appendRunEventPage } from './runEventPagination'
import * as RunsPageModule from './RunsPage'
import type { RunEventPage } from '../../types'

describe('运行事件诊断视图', () => {
  // 测试场景：详情面板提供事件类型、步骤和仅错误三种后端筛选条件。
  it('展示全部诊断筛选控件', () => {
    const FilterControls = Reflect.get(RunsPageModule, 'RunEventFilterControls')
    expect(FilterControls).toBeTypeOf('function')

    const markup = renderToStaticMarkup(createElement(FilterControls as ElementType, {
      filters: { event_type: '', step: undefined, errors_only: false, limit: 50 },
      onChange: vi.fn(),
    }))

    expect(markup).toContain('事件类型')
    expect(markup).toContain('步骤')
    expect(markup).toContain('仅看错误')
  })

  // 测试场景：加载更多按后端顺序追加事件，并采用新页返回的下一游标。
  it('追加游标分页结果', () => {
    const current = {
      items: [{ id: 'event-10', run_id: 'run-1', event_type: 'model_step_started', sequence: 10, step: 1, payload: {}, created_at: '2026-09-08T08:00:00Z' }],
      next_before: 10,
    } satisfies RunEventPage
    const incoming = {
      items: [{ id: 'event-9', run_id: 'run-1', event_type: 'model_retry', sequence: 9, step: 1, payload: {}, created_at: '2026-09-08T07:59:59Z' }],
      next_before: 9,
    } satisfies RunEventPage

    expect(appendRunEventPage(current, incoming)).toEqual({
      items: [...current.items, ...incoming.items],
      next_before: 9,
    })
  })

  it('去除游标页重叠事件', () => {
    const current = { items: [{ id: 'event-1', sequence: 1 }], next_before: 1 } as RunEventPage
    const incoming = { items: [{ id: 'event-1', sequence: 1 }, { id: 'event-0', sequence: 0 }], next_before: null } as RunEventPage
    expect(appendRunEventPage(current, incoming).items.map((event) => event.id)).toEqual(['event-1', 'event-0'])
  })
})
