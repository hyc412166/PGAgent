import { createContext } from 'react'
import type { DelegatedTask } from '../../types'

// 主会话中的委派活动与侧栏共用导航状态，不依赖 DOM 查询或名称匹配。
export const ChildNavigation = createContext<{
  tasks: DelegatedTask[]
  names: Record<string, string>
  detailId: string
  open: (id: string) => void
  back: () => void
} | null>(null)
