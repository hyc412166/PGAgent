export type SlidePanelState = { open: boolean; rendered: boolean }

// 关闭时保留 DOM 直到 transform 过渡结束；重新打开则立即保持渲染。
export function alignSlidePanelState(state: SlidePanelState, open: boolean): SlidePanelState {
  return state.open === open ? state : { open, rendered: open || state.rendered }
}

export function finishSlidePanelClosing(state: SlidePanelState): SlidePanelState {
  return state.open ? state : { open: false, rendered: false }
}
