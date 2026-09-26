// 本组件提供 Codex 式的小窗上下文：项目/分支是头部，来源和持久任务各自独立成区。
import { FileText, GitBranch, Globe2, Image, ListTodo, Link2, Plus, X } from 'lucide-react'

import type { DurableTask } from '../../../types'
import { orderDurableTasks } from '../sessionState'
import { StatusBadge } from '../../../components/ui'
import { DurableTaskCard } from './DurableTaskCard'

export type PersistentSourceItem = {
  id: string
  label: string
  kind: 'image' | 'document' | 'web'
  href: string
}

export function PersistentTaskSource({
  tasks,
  workspaceName = 'PGAgent',
  branchName,
  added,
  removed,
  sources = [],
  selectedTaskId,
  onClose,
  onSelectTask,
  onResume,
  onCancel,
  cancellingTaskId = '',
  resuming = false,
}: {
  tasks: DurableTask[]
  workspaceName?: string
  branchName?: string
  added?: number
  removed?: number
  sources?: PersistentSourceItem[]
  selectedTaskId?: string
  onClose?: () => void
  open?: boolean
  onToggle?: () => void
  onSelectTask: (taskId: string) => void
  onResume: (taskId: string) => void
  onCancel: (taskId: string) => void
  cancellingTaskId?: string
  resuming?: boolean
}) {
  const orderedTasks = orderDurableTasks(tasks)
  const selectedTask = orderedTasks.find((task) => task.id === selectedTaskId)
  const sourceIcon = (kind: PersistentSourceItem['kind']) => kind === 'image' ? <Image size={13} /> : kind === 'document' ? <FileText size={13} /> : <Globe2 size={13} />
  return <section className="persistent-task-source" aria-label="分支与来源及持久任务">
    <header className="persistent-source-header">
      <div className="persistent-source-project"><strong>{workspaceName}</strong><span className="persistent-source-branch-row"><GitBranch size={13} /><span>{branchName || 'main'}</span>{added !== undefined && <b className="change-added">+{added}</b>}{removed !== undefined && <b className="change-removed">-{removed}</b>}</span></div>
      {onClose && <button type="button" className="icon-button" onClick={onClose} aria-label="关闭会话上下文"><X size={14} /></button>}
    </header>
    {!!sources.length && <section className="persistent-source-list" aria-label="来源">
      <strong>来源</strong>
      {sources.map((source) => <a key={source.id} href={source.href} target="_blank" rel="noreferrer" className="persistent-source-row">
        {sourceIcon(source.kind)}<span>{source.label}</span><Link2 size={12} />
      </a>)}
    </section>}
    {!!orderedTasks.length && <section className="persistent-task-list" aria-label="持久任务">
      <div className="persistent-task-list-title"><ListTodo size={14} /><strong>持久任务</strong></div>
      {orderedTasks.map((task, index) => <button type="button" key={task.id} aria-expanded={task.id === selectedTaskId} className={`persistent-task-row${task.id === selectedTaskId ? ' is-selected' : ''}${index === 0 && task.status !== 'completed' ? ' is-pinned' : ''}`} onClick={() => onSelectTask(task.id)}>
        <span className="persistent-task-row-mark">{index === 0 && task.status !== 'completed' ? <Plus size={12} /> : index + 1}</span>
        <span><strong>{task.goal}</strong><small>{task.resume_summary || task.status}</small></span>
        <StatusBadge status={task.status} />
      </button>)}
    </section>}
    {selectedTask ? <div className="persistent-task-detail-popover">
      <DurableTaskCard
        task={selectedTask}
        cancelling={cancellingTaskId === selectedTask.id}
        resuming={resuming}
        onResume={() => onResume(selectedTask.id)}
        onCancel={() => onCancel(selectedTask.id)}
      />
    </div> : null}
  </section>
}
