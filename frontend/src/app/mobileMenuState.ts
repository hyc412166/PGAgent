export type MobileMenuState = { routeKey: string; open: boolean }

export function alignMobileMenuState(state: MobileMenuState, routeKey: string): MobileMenuState {
  return state.routeKey === routeKey ? state : { routeKey, open: false }
}

// 菜单只在打开它的 history 位置生效，任意路由跳转都会自然关闭。
export function isMobileMenuOpen(state: MobileMenuState, routeKey: string) {
  return state.open && state.routeKey === routeKey
}
