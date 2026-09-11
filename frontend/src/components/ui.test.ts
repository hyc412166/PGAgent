// 本测试验证侧滑面板的退出动画状态，关闭期间仍渲染，动画完成后才卸载。
import { describe, expect, it } from 'vitest'

import { alignSlidePanelState, finishSlidePanelClosing } from './slidePanelState'

describe('SlidePanel 退出动画', () => {
  it('关闭时保留渲染，过渡完成后卸载', () => {
    const closing = alignSlidePanelState({ open: true, rendered: true }, false)

    expect(closing).toEqual({ open: false, rendered: true })
    expect(finishSlidePanelClosing(closing)).toEqual({ open: false, rendered: false })
  })

  it('退出期间重新打开时继续渲染', () => {
    const reopened = alignSlidePanelState({ open: false, rendered: true }, true)

    expect(reopened).toEqual({ open: true, rendered: true })
    expect(finishSlidePanelClosing(reopened)).toBe(reopened)
  })
})
