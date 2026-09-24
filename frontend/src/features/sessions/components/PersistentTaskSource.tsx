// 本组件把分支、来源和持久任务收敛到子 Agent 详情下的一个可展开入口。
import { ChevronRight, FileText, GitBranch, Globe2, Image, ListTodo, Link2, Plus, X } from 'lucide-react'

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
  branchName,
  added,
  removed,
  sources = [],
  selectedTaskId,
  open,
  onToggle,
  onSelectTask,
  onResume,
  onCancel,
  cancellingTaskId = '',
  resuming = false,
}: {
  tasks: DurableTask[]
  branchName?: string
  added?: number
  removed?: number
  sources?: PersistentSourceItem[]
  selectedTaskId?: string
  open: boolean
  onToggle: () => void
  onSelectTask: (taskId: string) => void
  onResume: (taskId: string) => void
  onCancel: (taskId: string) => void
  cancellingTaskId?: string
  resuming?: boolean
}) {
  const orderedTasks = orderDurableTasks(tasks)
  const selectedTask = orderedTasks.find((task) => task.id === selectedTaskId)
  const sourceIcon = (kind: PersistentSourceItem['kind']) => kind === 'image' ? <Image size={13} /> : kind === 'document' ? <FileText size={13} /> : <Globe2 size={13} />
  return <section className="persistent-task-source" aria-label="分支与来源">
    <button type="button" className={`persistent-source-trigger${open ? ' is-open' : ''}`} onClick={onToggle} aria-expanded={open}>
      <span className="persistent-source-trigger-icon"><GitBranch size={14} /></span>
      <span><strong>分支与来源</strong><small>{branchName || '共享工作区'} · {tasks.length} 个持久任务</small></span>
      <ChevronRight className="persistent-source-chevron" size={15} />
    </button>
    {open ? <div className="persistent-source-popover">
      <header className="persistent-source-header">
        <div><span className="eyebrow">工作摘要</span><strong>{branchName || '共享工作区'}</strong></div>
        <button type="button" className="icon-button" onClick={onToggle} aria-label="收起分支与来源"><X size={14} /></button>
      </header>
      {branchName && <section className="persistent-source-branch" aria-label="分支变更">
        <div className="persistent-source-branch-row"><GitBranch size={14} /><span>{branchName}</span>{added !== undefined && <b className="change-added">+{added}</b>}{removed !== undefined && <b className="change-removed">-{removed}</b>}</div>
      </section>}
      {!!sources.length && <section className="persistent-source-list" aria-label="来源">
        <strong>来源</strong>
        {sources.map((source) => <a key={source.id} href={source.href} target="_blank" rel="noreferrer" className="persistent-source-row">
          {sourceIcon(source.kind)}<span>{source.label}</span><Link2 size={12} />
        </a>)}
      </section>}
      {!!orderedTasks.length && <section className="persistent-task-list" aria-label="持久任务">
        <div className="persistent-task-list-title"><ListTodo size={14} /><strong>持久任务</strong></div>
        {orderedTasks.map((task, index) => <button type="button" key={task.id} className={`persistent-task-row${task.id === selectedTaskId ? ' is-selected' : ''}${index === 0 && task.status !== 'completed' ? ' is-pinned' : ''}`} onClick={() => onSelectTask(task.id)}>
          <span className="persistent-task-row-mark">{index === 0 && task.status !== 'completed' ? <Plus size={12} /> : index + 1}</span>
          <span><strong>{task.goal}</strong><small>{task.resume_summary || task.status}</small></span>
          <StatusBadge status={task.status} />
        </button>)}
      </section>}
      {selectedTask ? <div className="persistent-task-detail-popover">
        <DurableTaskCard
          task={selectedTask}
          compact
          cancelling={cancellingTaskId === selectedTask.id}
          resuming={resuming}
          onResume={() => onResume(selectedTask.id)}
          onCancel={() => onCancel(selectedTask.id)}
        />
      </div> : null}
    </div> : null}
  </section>
}
