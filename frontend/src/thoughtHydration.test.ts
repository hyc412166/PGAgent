import { describe, expect, it } from 'vitest'
import { ThoughtHydrationRegistry } from './thoughtHydration'

describe('历史思考加载登记', () => {
  it('取消的旧请求不会冒充已加载，也不能清除同一 Run 的新请求', () => {
    const registry = new ThoughtHydrationRegistry()
    const first = registry.begin('run-1')
    expect(first).toBeTruthy()
    expect(registry.begin('run-1')).toBeNull()

    registry.cancel('run-1', first!)
    const second = registry.begin('run-1')
    expect(second).toBeTruthy()

    registry.cancel('run-1', first!)
    expect(registry.isPending('run-1')).toBe(true)
    expect(registry.complete('run-1', second!)).toBe(true)
    expect(registry.isLoaded('run-1')).toBe(true)
  })

  it('只有当前令牌对应的成功请求才能提交，reset 后允许会话重新加载', () => {
    const registry = new ThoughtHydrationRegistry()
    const token = registry.begin('run-2')!
    expect(registry.complete('run-2', Symbol('stale'))).toBe(false)
    expect(registry.isLoaded('run-2')).toBe(false)
    expect(registry.complete('run-2', token)).toBe(true)
    expect(registry.shouldLoad('run-2')).toBe(false)

    registry.reset()
    expect(registry.shouldLoad('run-2')).toBe(true)
  })
})
