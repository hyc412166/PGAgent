import { Check, LoaderCircle, Play, Workflow, X } from 'lucide-react'

import { StatusBadge } from '../../../components/ui'
import type { DurableTask } from '../../../types'

const taskStatusLabels: Record<string, string> = {
  planning: '正在规划',
  running: '执行中',
  paused: '已暂停',
  needs_recovery: '需要恢复核验',
  blocked: '等待处理',
}
export function DurableTaskCard({
  task,
  onResume,
  onCancel,
  cancelling = false,
  resuming = false,
}: {
  task: DurableTask
  onResume: () => void
  onCancel: () => void
  cancelling?: boolean
  resuming?: boolean
}) {
  const resumable = task.status === 'paused' || task.status === 'needs_recovery' || task.status === 'blocked'
  const cancellable = ['planning', 'running', 'waiting', 'paused', 'needs_recovery', 'blocked'].includes(task.status)
  return <article className={`durable-task-card task-${task.status}`}>
    <header>
      <span className="durable-task-mark"><Workflow size={15} /></span>
      <div><small>持久化任务</small><strong>{task.goal}</strong></div>
      <StatusBadge status={taskStatusLabels[task.status] || task.status} />
    </header>
    <ol className="durable-plan-track">
      {task.steps.map((step) => <li key={step.id} className={`step-${step.status}`}>
        <span className="plan-step-node">{step.status === 'completed' ? <Check size={11} /> : step.position}</span>
        <div><strong>{step.title}</strong><span className="durable-step-meta">{step.executor_kind === 'subagent' ? '子 Agent' : step.executor_kind === 'background' ? '后台' : '主 Agent'}{step.depends_on?.length ? ` · 依赖 ${step.depends_on.join(', ')}` : ' · 可立即执行'}{step.workspace_mode === 'worktree' ? ' · 独立 worktree' : ''}</span>{step.next_action && step.status !== 'completed' ? <small>{step.next_action}</small> : null}</div>
      </li>)}
    </ol>
    {task.resume_summary ? <p className="durable-task-summary">{task.resume_summary}</p> : null}
    {cancellable ? <div className="durable-task-actions">
      {resumable ? <button type="button" className="durable-task-resume" onClick={onResume} disabled={cancelling || resuming} aria-busy={resuming}>{resuming ? <LoaderCircle className="spin" size={12} /> : <Play size={12} />}{resuming ? '正在继续…' : '继续此任务'}</button> : null}
      <button type="button" className="durable-task-cancel" onClick={onCancel} disabled={cancelling} aria-busy={cancelling}>
        {cancelling ? <LoaderCircle className="spin" size={12} /> : <X size={12} />}{cancelling ? '正在取消…' : '取消任务'}
      </button>
    </div> : null}
  </article>
}
