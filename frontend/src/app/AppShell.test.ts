// 本测试验证移动导航只属于打开它的那一次路由位置。
import { describe, expect, it } from 'vitest'

import { alignMobileMenuState, isMobileMenuOpen } from './mobileMenuState'

describe('AppShell 移动导航', () => {
  it('路由位置变化后关闭菜单', () => {
    const menu = { routeKey: 'route-a', open: true }

    expect(isMobileMenuOpen(menu, 'route-a')).toBe(true)
    const afterNavigation = alignMobileMenuState(menu, 'route-b')
    expect(isMobileMenuOpen(afterNavigation, 'route-b')).toBe(false)

    const afterBackNavigation = alignMobileMenuState(afterNavigation, 'route-a')
    expect(isMobileMenuOpen(afterBackNavigation, 'route-a')).toBe(false)
  })
})
