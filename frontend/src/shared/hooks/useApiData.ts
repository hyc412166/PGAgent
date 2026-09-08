// 本文件负责 useApiData 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import { useCallback, useEffect, useRef, useState } from 'react'

import { describeError } from '../../api'

// LoadState 保存资源数据、请求状态和可展示错误。
type LoadState<T> = { data: T; loading: boolean; error: string }

// useApiData 为页面提供首次加载、静默刷新及外部覆写；请求序号防止旧响应覆盖新状态。
export function useApiData<T>(initial: T, loader: () => Promise<T>, deps: readonly unknown[] = []) {
  const [state, setState] = useState<LoadState<T>>({ data: initial, loading: true, error: '' })
  // requestId 标识最新请求，hasLoaded 区分首次骨架屏与后台刷新。
  const requestId = useRef(0)
  const hasLoaded = useRef(false)
  const isInitialLoad = !hasLoaded.current
  // 外部写入会使在途请求失效，避免其稍后覆盖调用方同步的权威状态。
  const setExternalState = useCallback((next: LoadState<T>) => {
    requestId.current += 1
    setState(next)
  }, [])

  // reload 显式进入 loading 并展示错误，适合首次加载和用户点击重试。
  const reload = useCallback(async () => {
    const currentRequest = ++requestId.current
    setState((previous) => ({ ...previous, loading: true, error: '' }))
    try {
      const data = await loader()
      if (currentRequest === requestId.current) {
        hasLoaded.current = true
        setState({ data, loading: false, error: '' })
      }
    } catch (error) {
      if (currentRequest === requestId.current) setState((previous) => ({ ...previous, loading: false, error: describeError(error) }))
    }
  // loader 的身份由调用方传入的 deps 表示，避免内联函数导致每次渲染重新请求。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  // refresh 保留已有数据；已有成功数据时后台刷新失败不会把页面替换成错误态。
  const refresh = useCallback(async () => {
    const currentRequest = ++requestId.current
    try {
      const data = await loader()
      if (currentRequest === requestId.current) {
        hasLoaded.current = true
        setState({ data, loading: false, error: '' })
        return data
      }
    } catch (error) {
      if (currentRequest === requestId.current && !hasLoaded.current) {
        setState((previous) => ({ ...previous, loading: false, error: describeError(error) }))
      }
      return undefined
    }
  // loader 的身份由调用方传入的 deps 表示。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    void reload()
    return () => { requestId.current += 1 }
  }, [reload])

  return { ...state, initialLoading: state.loading && isInitialLoad, refreshing: state.loading && !isInitialLoad, reload, refresh, setState: setExternalState }
}
