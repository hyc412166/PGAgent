import {
  Brain,
  Bot,
  CheckCheck,
  ChevronRight,
  Copy,
  FileText,
  Globe2,
  LoaderCircle,
  Pencil,
  Search,
  ShieldCheck,
  SquareTerminal,
  Users,
  Wrench,
  X,
  XCircle,
  CheckCircle2,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { memo } from 'react'
import { formatLiveThinkingDuration, formatThoughtDuration, type ThoughtActivityIcon, type ThoughtActivityItem, type ThoughtTimelineState } from '../../thoughtTimeline'
import { apiUrl } from '../../api'
import { formatAttachmentSize, messageAttachments } from '../../attachments'
import type { Approval, DelegatedTask, Message, Run, RunEvent, Teammate } from '../../types'
import { EmptyState, ErrorState, LoadingState, StatusBadge } from '../../components/ui'
import { statusText } from '../../components/status'
import { PenguinMark } from '../../components/penguin'

export type LiveRunView = {
  runId: string
  phase: string
  draft: string
  status: 'idle' | 'connecting' | 'live' | 'fallback' | 'awaiting_approval' | 'terminal'
  error: string
  thought: ThoughtTimelineState
  thinkingStatus: string
}

function formatUiDate(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

function numberFromRecord(record: Record<string, unknown> | undefined, key: string) {
  const value = record?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function childTaskStatusLabel(status?: string) {
  return statusText[(status || '').toLowerCase()] ?? status ?? '处理中'
}

function childTaskOutput(task?: DelegatedTask) {
  const output = task?.result?.output
  if (typeof output === 'string' && output.trim()) return output
  const error = task?.result?.error
  if (typeof error === 'string' && error.trim()) return error
  return ''
}

export const ChildAgentPanel = memo(function ChildAgentPanel({
  open,
  tasks,
  teammates,
  loading,
  error,
  selectedTask,
  run,
  events,
  eventsLoading,
  eventsError,
  onClose,
  onSelect,
  onRetry,
}: {
  open: boolean
  tasks: DelegatedTask[]
  teammates: Teammate[]
  loading: boolean
  error: string
  selectedTask?: DelegatedTask
  run?: Run
  events: RunEvent[]
  eventsLoading: boolean
  eventsError: string
  onClose: () => void
  onSelect: (taskId: string) => void
  onRetry: () => void
}) {
  const output = childTaskOutput(selectedTask)
  const toolEvents = events.filter((event) => ['tool_started', 'tool_finished', 'tool_result'].includes(event.type || event.event_type || ''))
  return <aside className={`child-agent-panel ${open ? 'is-open' : 'is-closed'}`} aria-label="子 Agent 工作详情" aria-hidden={!open} inert={!open}>
    <header className="child-panel-header"><div><span className="eyebrow">协作执行</span><strong>子 Agent</strong></div><button type="button" className="icon-button" onClick={onClose} aria-label="收起子 Agent 侧栏"><X size={16} /></button></header>
    {!!teammates.length && <section className="teammate-roster" aria-label="持久化队友"><strong>协作队友</strong><div>{teammates.map((teammate) => <span key={teammate.id} className={`teammate teammate-${teammate.status}`} title={teammate.branch_name || teammate.worktree_path || '共享工作区'}><Bot size={12} /><b>{teammate.name}</b><small>{teammate.status}{teammate.workspace_mode === 'worktree' ? ' · worktree' : ''}</small></span>)}</div></section>}
    {error ? <ErrorState message={error} onRetry={onRetry} /> : loading && !tasks.length ? <LoadingState label="正在读取子 Agent…" /> : !tasks.length ? <EmptyState icon={Users} title="尚未调用子 Agent" description="主 Agent 发起委派后，真实的子 Agent 任务会显示在这里。" /> : <>
      <div className="child-task-list" role="list" aria-label="本次调用的子 Agent">
        {tasks.map((task) => {
          const selected = task.id === selectedTask?.id
          const agent = task.result?.agent
          const agentName = agent && typeof agent === 'object' && typeof (agent as Record<string, unknown>).name === 'string'
            ? String((agent as Record<string, unknown>).name)
            : task.child_agent_name || '子 Agent'
          return <button key={task.id} type="button" className={selected ? 'selected' : ''} onClick={() => onSelect(task.id)}>
            <span className="child-task-avatar"><Bot size={14} /></span><span><strong>{agentName}</strong><small>{task.title}</small></span><StatusBadge status={task.status} />
          </button>
        })}
      </div>
      {selectedTask && <section className="child-task-detail">
        <header><div><strong>{selectedTask.title}</strong><small>{childTaskStatusLabel(selectedTask.status)}</small></div><StatusBadge status={selectedTask.status} /></header>
        <dl className="child-task-stats"><div><dt>步骤</dt><dd>{numberFromRecord(selectedTask.result, 'steps') ?? run?.step_count ?? run?.current_step ?? 0}</dd></div><div><dt>工具</dt><dd>{numberFromRecord(selectedTask.result, 'tool_calls') ?? run?.tool_calls ?? 0}</dd></div></dl>
        {selectedTask.description && <section className="child-detail-block"><strong>任务</strong><p>{selectedTask.description}</p></section>}
        {output && <section className="child-detail-block"><strong>{selectedTask.status === 'completed' ? '结果' : '状态说明'}</strong><pre>{output}</pre></section>}
        <section className="child-detail-block child-events"><strong>工作过程</strong>{eventsError ? <p className="inline-error">{eventsError}</p> : eventsLoading ? <p>正在读取运行事件…</p> : toolEvents.length ? <ol>{toolEvents.map((event, index) => <li key={event.id || index}><span>{event.type || event.event_type}</span><small>{formatUiDate(event.created_at)}</small></li>)}</ol> : <p>暂未记录工具调用。</p>}</section>
      </section>}
    </>}
  </aside>
})

export const MessageBubble = memo(function MessageBubble({ message }: { message: Message }) {
  const [copied, setCopied] = useState(false)
  const attachments = messageAttachments(message.metadata?.attachments)
  const imageAttachments = message.session_id
    ? attachments.filter((attachment) => attachment.mime_type.startsWith('image/'))
    : []
  const fileAttachments = attachments.filter((attachment) => (
    !attachment.mime_type.startsWith('image/') || !message.session_id
  ))
  const isTool = message.role === 'tool' || !!message.tool_name
  const isDelegatedChild = message.metadata?.delegated_child === true
  const childAgentName = typeof message.metadata?.child_agent_name === 'string'
    ? message.metadata.child_agent_name
    : '子 Agent'
  const speaker = message.role === 'user'
    ? '你'
    : isTool
      ? message.tool_name || '工具结果'
      : isDelegatedChild
        ? `${childAgentName}（子 Agent）`
        : 'PGAgent'
  async function copyMessage() {
    try {
      await navigator.clipboard.writeText(message.content || '')
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1400)
    } catch {
      setCopied(false)
    }
  }
  return (
    <article className={`message ${message.role} ${isTool ? 'tool-message' : ''} ${attachments.length ? 'has-attachments' : ''}`}>
      <div className="message-avatar">{message.role === 'user' ? '你' : isTool ? <SquareTerminal size={16} /> : <PenguinMark size={21} />}</div>
      <div className="message-body">
        <div className="message-meta"><strong>{speaker}</strong><time>{formatUiDate(message.created_at)}</time></div>
        {!!imageAttachments.length && <div className="message-image-gallery" aria-label="消息图片">
          {imageAttachments.map((attachment) => {
            const href = apiUrl(`/api/sessions/${message.session_id}/attachments/${attachment.id}/content`)
            return <a key={attachment.id} className="message-image-link" href={href} target="_blank" rel="noreferrer" title={`打开 ${attachment.name}`}>
              <img src={href} alt={attachment.name} loading="lazy" />
            </a>
          })}
        </div>}
        {!!message.content && <div className="message-content">{message.content}</div>}
        {!!message.citations?.length && <div className="message-citations" aria-label="参考来源">
          <strong>参考来源</strong>
          {message.citations.map((citation, index) => <a key={`${citation.url}-${index}`} href={citation.url} target="_blank" rel="noreferrer">
            <span>{index + 1}</span>{citation.title || citation.url}
          </a>)}
        </div>}
        {!!fileAttachments.length && <div className="message-attachments" aria-label="消息附件">
          {fileAttachments.map((attachment) => {
            const href = message.session_id
              ? apiUrl(`/api/sessions/${message.session_id}/attachments/${attachment.id}/content`)
              : ''
            return href ? <a key={attachment.id} className="message-attachment" href={href} target="_blank" rel="noreferrer" title={`打开 ${attachment.name}`}>
              <span className="message-attachment-preview"><FileText size={18} /></span>
              <span><strong>{attachment.name}</strong><small>{attachment.mime_type === 'application/pdf' ? 'PDF' : attachment.mime_type} · {formatAttachmentSize(attachment.size_bytes)}</small></span>
            </a> : <span key={attachment.id} className="message-attachment">
              <span className="message-attachment-preview"><FileText size={18} /></span>
              <span><strong>{attachment.name}</strong><small>{formatAttachmentSize(attachment.size_bytes)}</small></span>
            </span>
          })}
        </div>}
        {message.status && <StatusBadge status={message.status} />}
        <div className="message-actions">
          <button type="button" className="message-copy-button" aria-label={copied ? '已复制' : '复制消息'} title={copied ? '已复制' : '复制消息'} onClick={() => void copyMessage()}>{copied ? <CheckCheck size={11} /> : <Copy size={11} />}</button>
        </div>
      </div>
    </article>
  )
})

function ThoughtActivityIcon({ icon }: { icon: ThoughtActivityIcon }) {
  if (icon === 'task') return <span className="thought-activity-icon is-task"><PenguinMark size={13} /></span>
  if (icon === 'search') return <span className="thought-activity-icon is-search"><Search size={13} /></span>
  if (icon === 'read') return <span className="thought-activity-icon is-read"><FileText size={13} /></span>
  if (icon === 'write' || icon === 'edit') return <span className="thought-activity-icon is-write"><Pencil size={13} /></span>
  if (icon === 'shell') return <span className="thought-activity-icon is-shell"><SquareTerminal size={13} /></span>
  if (icon === 'approval') return <span className="thought-activity-icon is-approval"><ShieldCheck size={13} /></span>
  if (icon === 'context') return <span className="thought-activity-icon is-context"><Globe2 size={13} /></span>
  if (icon === 'think') return <span className="thought-activity-icon is-think"><Brain size={13} /></span>
  return <span className="thought-activity-icon is-generic"><Wrench size={13} /></span>
}

function fallbackThoughtItems(timeline: ThoughtTimelineState): ThoughtActivityItem[] {
  if (timeline.items?.length) return timeline.items
  return timeline.tools.map((tool) => ({
    id: tool.id,
    kind: 'tool' as const,
    icon: 'generic' as const,
    title: tool.name,
    detail: tool.target,
    status: tool.status,
  }))
}

function hasActivityDetails(items: ThoughtActivityItem[]) {
  // A generic model-step marker is useful for the live phase/timer, but it
  // is not expandable content by itself.  Show the disclosure only when a
  // real safe progress summary or tool/context activity exists.
  return items.some((item) => Boolean(item.detail.trim()) || item.kind !== 'thought')
}

function ThoughtActivityList({ items, live = false, activeItemId }: { items: ThoughtActivityItem[]; live?: boolean; activeItemId?: string }) {
  if (!items.length) return null
  return <div className={`thought-activity-list ${live ? 'is-live' : ''}`} aria-label="执行详情">
    {items.map((item) => <div key={item.id} className={`thought-activity kind-${item.kind} ${item.status} ${live && item.status === 'running' && item.id === activeItemId ? 'is-active' : ''}`}>
      <ThoughtActivityIcon icon={item.icon} />
      <div className="thought-activity-copy"><strong>{item.title}</strong>{item.detail && <span title={item.detail}>{item.detail}</span>}</div>
    </div>)}
  </div>
}

export const CompletedThoughtTimeline = memo(function CompletedThoughtTimeline({ runId, timeline }: { runId: string; timeline: ThoughtTimelineState }) {
  const [expanded, setExpanded] = useState(false)
  const duration = formatThoughtDuration(timeline.elapsedMs)
  const items = fallbackThoughtItems(timeline)
  const hasDetails = hasActivityDetails(items)
  const summary = timeline.conclusion || '执行完成'

  return <article className={`completed-thought ${hasDetails && expanded ? 'expanded' : ''} ${hasDetails ? '' : 'no-details'}`}>
    {hasDetails ? <button type="button" className="completed-thought-toggle" aria-expanded={expanded} aria-controls={`thought-details-${runId}`} onClick={() => setExpanded((value) => !value)}>
      <span className="completed-thought-duration">执行详情 · 用时 {duration}</span><span className="completed-thought-summary">{summary}</span><ChevronRight className="completed-thought-chevron" size={13} aria-hidden="true" />
    </button> : <span className="completed-thought-duration completed-thought-static">用时 {duration}</span>}
    {hasDetails && expanded && <div id={`thought-details-${runId}`}><ThoughtActivityList items={items} /></div>}
  </article>
})

export const LiveAssistantMessage = memo(function LiveAssistantMessage({ liveRun }: { liveRun: LiveRunView }) {
  const [now, setNow] = useState(() => Date.now())
  const [expanded, setExpanded] = useState(true)
  useEffect(() => {
    if (liveRun.thought.startedAt === null || liveRun.thought.finished) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [liveRun.thought.finished, liveRun.thought.startedAt])
  useEffect(() => {
    setExpanded(true)
  }, [liveRun.runId])
  useEffect(() => {
    if (liveRun.thought.finished || liveRun.status === 'terminal') setExpanded(false)
  }, [liveRun.status, liveRun.thought.finished])
  const liveThoughtMs = liveRun.thought.startedAt === null ? 0 : Math.max(0, now - liveRun.thought.startedAt)
  const operationalPhase = liveRun.phase.startsWith('正在连接 MCP') || liveRun.phase.startsWith('MCP ')
  const phase = liveRun.phase.includes('子 Agent')
    ? liveRun.phase
    : operationalPhase
      ? liveRun.phase
    : !liveRun.thought.finished && liveRun.status !== 'awaiting_approval' && liveRun.thinkingStatus
      ? `${liveRun.thinkingStatus} ${formatLiveThinkingDuration(liveThoughtMs)}`
      : liveRun.phase || '已完成'
  const items = fallbackThoughtItems(liveRun.thought)
  const hasDetails = hasActivityDetails(items)
  return (
    <article className={`message assistant live-message ${liveRun.status === 'terminal' ? 'live-message-terminal' : ''}`}>
      <div className="message-avatar"><PenguinMark size={21} /></div>
      <div className="message-body"><div className="message-meta"><strong>PGAgent</strong><span className="live-phase">{phase}</span></div>
        {hasDetails && <div className={`live-thought ${expanded ? 'expanded' : ''}`}>
          <button type="button" className="live-thought-toggle" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}><ChevronRight className="live-thought-chevron" size={13} aria-hidden="true" /><span>{liveRun.thought.finished ? `执行详情 · 用时 ${formatThoughtDuration(liveRun.thought.elapsedMs)}` : '执行详情'}</span></button>
          {expanded && <ThoughtActivityList items={items} live activeItemId={liveRun.thought.activeItemId} />}
        </div>}
        {liveRun.draft && <div className="message-content">{liveRun.draft}</div>}{liveRun.error && <p className="live-error">{liveRun.error}</p>}
      </div>
    </article>
  )
})

export const ApprovalCard = memo(function ApprovalCard({ approval, deciding, onDecision }: { approval: Approval; deciding: boolean; onDecision: (id: string, decision: 'approve' | 'reject', runId: string) => void }) {
  const runId = typeof approval.run_id === 'string' || typeof approval.run_id === 'number' ? String(approval.run_id) : ''
  return (
    <article className="approval-card">
      <header><span><ShieldCheck size={17} /></span><div><strong>需要你的批准</strong><p>Agent 请求执行有副作用的工具</p></div><StatusBadge status={approval.status || 'pending'} /></header>
      <div className="approval-command"><span>{approval.tool_name || 'unknown_tool'}</span><pre>{JSON.stringify(approval.arguments ?? {}, null, 2)}</pre></div>
      {approval.reason && <p className="approval-reason">理由：{approval.reason}</p>}
      <footer><button className="button button-danger" disabled={deciding || !runId} onClick={() => onDecision(approval.id, 'reject', runId)}><XCircle size={15} />拒绝</button><button className="button button-primary" disabled={deciding || !runId} onClick={() => onDecision(approval.id, 'approve', runId)}>{deciding ? <LoaderCircle className="spin" size={15} /> : <CheckCircle2 size={15} />}允许本次</button></footer>
    </article>
  )
})
