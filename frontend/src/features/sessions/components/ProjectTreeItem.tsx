// 本文件实现 ProjectTreeItem 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
import { ChevronRight, Folder, LoaderCircle, Trash2 } from 'lucide-react'
import type { FocusEvent, MouseEvent, ReactNode } from 'react'
import type { Workspace } from '../../../types'

// ProjectTreeItemProps 包含项目节点、其会话子项以及悬浮/删除交互回调。
type ProjectTreeItemProps = {
  workspace: Workspace
  expanded: boolean
  deleting: boolean
  deleteDisabled: boolean
  hoverCardVisible: boolean
  children: ReactNode
  onToggle: () => void
  onDelete: () => void
  onShowHoverCard: (event: MouseEvent<HTMLButtonElement> | FocusEvent<HTMLButtonElement>) => void
  onHideHoverCard: () => void
}

// ProjectTreeItem 渲染可展开项目节点，并将子会话内容交由 renderSession 回调生成。
export function ProjectTreeItem({
  workspace,
  expanded,
  deleting,
  deleteDisabled,
  hoverCardVisible,
  children,
  onToggle,
  onDelete,
  onShowHoverCard,
  onHideHoverCard,
}: ProjectTreeItemProps) {
  return <div className={`project-node ${expanded ? 'expanded' : ''}`}>
    <div className="project-row-wrap" onMouseLeave={onHideHoverCard}>
      <button
        type="button"
        className="project-row"
        aria-expanded={expanded}
        aria-describedby={hoverCardVisible ? 'project-hover-card' : undefined}
        onMouseEnter={onShowHoverCard}
        onFocus={onShowHoverCard}
        onBlur={onHideHoverCard}
        onClick={onToggle}
      >
        <ChevronRight className="project-chevron" size={13} />
        <Folder size={15} />
        <span>{workspace.name}</span>
      </button>
      <button
        type="button"
        className="project-delete"
        aria-label={`删除项目 ${workspace.name}`}
        title="从 PGAgent 删除项目"
        disabled={deleteDisabled}
        onClick={onDelete}
      >
        {deleting ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}
      </button>
    </div>
    {expanded ? children : null}
  </div>
}
