import { useCallback, useEffect, useRef, useState } from 'react'

import { describeError } from '../../api'

type LoadState<T> = { data: T; loading: boolean; error: string }

export function useApiData<T>(initial: T, loader: () => Promise<T>, deps: readonly unknown[] = []) {
  const [state, setState] = useState<LoadState<T>>({ data: initial, loading: true, error: '' })
  const requestId = useRef(0)
  const hasLoaded = useRef(false)
  const isInitialLoad = !hasLoaded.current
  const setExternalState = useCallback((next: LoadState<T>) => {
    requestId.current += 1
    setState(next)
  }, [])

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
  // Loader identity is intentionally represented by the caller-owned deps.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

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
  // Loader identity is intentionally represented by the caller-owned deps.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    void reload()
    return () => { requestId.current += 1 }
  }, [reload])

  return { ...state, initialLoading: state.loading && isInitialLoad, refreshing: state.loading && !isInitialLoad, reload, refresh, setState: setExternalState }
}
