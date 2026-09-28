export function turnScrollDuration(distance: number, viewportHeight: number, reducedMotion: boolean): number {
  return !reducedMotion && viewportHeight > 0 && Math.abs(distance) <= viewportHeight * 4 ? 600 : 0
}

// 每帧重算终点，避免历史详情的高度变化使动画落在旧坐标；只滚动消息容器。
export function scrollToTurnTarget(container: HTMLElement, target: HTMLElement, reducedMotion: boolean): () => void {
  // 屏幕外消息的占位高度不能用于导航距离；定位期间先完成真实布局。
  container.classList.add('is-locating-turn')
  const destination = () => {
    const rect = target.getBoundingClientRect()
    const top = container.scrollTop + rect.top - container.getBoundingClientRect().top - container.clientTop
      + rect.height / 2 - container.clientHeight / 2
    return Math.max(0, Math.min(top, container.scrollHeight - container.clientHeight))
  }
  const start = container.scrollTop
  const duration = turnScrollDuration(destination() - start, container.clientHeight, reducedMotion)
  let frame = 0
  const cancel = () => {
    window.cancelAnimationFrame(frame)
    container.classList.remove('is-locating-turn')
    container.removeEventListener('wheel', cancel)
    container.removeEventListener('touchstart', cancel)
    container.removeEventListener('pointerdown', cancel)
    container.removeEventListener('keydown', cancel)
  }
  if (!duration) {
    container.scrollTo({ top: destination(), behavior: 'instant' })
    frame = window.requestAnimationFrame(() => {
      container.scrollTo({ top: destination(), behavior: 'instant' })
      cancel()
    })
    return cancel
  }
  const startedAt = performance.now()
  const tick = (now: number) => {
    const progress = Math.min(1, (now - startedAt) / duration)
    const eased = 1 - (1 - progress) ** 3
    container.scrollTo({ top: start + (destination() - start) * eased, behavior: 'instant' })
    if (progress < 1) frame = window.requestAnimationFrame(tick)
    else cancel()
  }
  container.addEventListener('wheel', cancel, { passive: true })
  container.addEventListener('touchstart', cancel, { passive: true })
  container.addEventListener('pointerdown', cancel)
  container.addEventListener('keydown', cancel)
  frame = window.requestAnimationFrame(tick)
  return cancel
}
