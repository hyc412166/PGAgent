// 本文件实现 presentation 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
import {
  Brain,
  Bot,
  CheckCheck,
  ChevronDown,
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
import type { ReactNode } from 'react'
import { formatLiveThinkingDuration, formatThoughtDuration, parseFileChangeSet, type ThoughtActivityIcon, type ThoughtActivityItem, type ThoughtTimelineState } from '../../thoughtTimeline'
import { apiUrl, describeError } from '../../api'
import { formatAttachmentSize, messageAttachments } from '../../attachments'
import type { Approval, DelegatedTask, DurableTask, FileChangeRecord, FileChangeSelection, FileChangeSet, Message, Run, RunEvent, Teammate } from '../../types'
import { EmptyState, ErrorState, LoadingState, StatusBadge } from '../../components/ui'
import { statusText } from '../../components/status'
import { PenguinMark } from '../../components/penguin'
import { isHiddenRunEvent, presentRunEvent } from '../../runEventPresentation'
import { MarkdownContent } from './MarkdownContent'
import { groupThoughtActivities } from './thoughtActivityGrouping'
import { formatApprovalArguments } from './approvalPresentation'
import { createToolResultDetailLoader, presentToolResult, toolResultRequest, type CompleteToolResult } from '../../toolResultDetails'
import type { RunPlanStep } from './sessionState'
import { PersistentTaskSource } from './components/PersistentTaskSource'

// LiveRunView 是运输状态到实时回复组件之间的最小只读接口。
export type LiveRunView = {
  runId: string
  phase: string
  draft: string
  assistantItems?: Array<{ id: string; content: string; status: 'streaming' | 'completed'; responseId?: string; itemId?: string; outputIndex?: number; phase?: string }>
  plan?: RunPlanStep[]
  status: 'idle' | 'connecting' | 'live' | 'fallback' | 'awaiting_approval' | 'terminal'
  error: string
  thought: ThoughtTimelineState
  thinkingStatus: string
}

// RunPlanProgress 只呈现当前运行的轻量检查清单，不提供持久任务的暂停、恢复或取消语义。
export function RunPlanProgress({ plan }: { plan: RunPlanStep[] }) {
  if (!plan.length) return null
  const completed = plan.filter((step) => step.status === 'completed').length
  return <section className="run-plan-progress" aria-label="本次运行任务进度">
    <header><strong>任务进度</strong><span>{completed}/{plan.length}</span></header>
    <ol>{plan.map((step, index) => <li key={step.id} className={`run-plan-step step-${step.status}`}>
      <span className="run-plan-node" aria-hidden="true">
        {step.status === 'completed' ? <CheckCircle2 size={14} /> : step.status === 'in_progress' ? <LoaderCircle className="spin" size={14} /> : step.status === 'cancelled' ? <XCircle size={14} /> : index + 1}
      </span>
      <span>{step.content}</span>
    </li>)}</ol>
  </section>
}

// 将后端时间转换为当前语言环境下的短日期时间。
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

// 从宽松结果对象中安全读取数值字段。
function numberFromRecord(record: Record<string, unknown> | undefined, key: string) {
  const value = record?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

// 将子任务内部状态转换为面板标签。
function childTaskStatusLabel(status?: string) {
  return statusText[(status || '').toLowerCase()] ?? status ?? '处理中'
}

// 按优先级提取子任务最终输出、错误或当前阶段说明。
function childTaskOutput(_task?: DelegatedTask) {
  // 子任务原始结果可能包含文件正文、命令输出或 provider 错误；仅使用 RunEvent 的安全摘要。
  return ''
}

// ChildAgentPanel 展示协作树、选中子任务详情及其运行事件，memo 避免流式文本更新带来无关重绘。
export const ChildAgentPanel = memo(function ChildAgentPanel({
  open,
  tasks,
  durableTasks = [],
  selectedDurableTaskId,
  durableSourceOpen = false,
  onToggleDurableSource = () => undefined,
  onSelectDurableTask = () => undefined,
  onResumeDurableTask = () => undefined,
  onCancelDurableTask = () => undefined,
  cancellingDurableTaskId,
  resumingDurableTask,
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
  durableTasks?: DurableTask[]
  selectedDurableTaskId?: string
  durableSourceOpen?: boolean
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
  onToggleDurableSource?: () => void
  onSelectDurableTask?: (taskId: string) => void
  onResumeDurableTask?: (taskId: string) => void
  onCancelDurableTask?: (taskId: string) => void
  cancellingDurableTaskId?: string
  resumingDurableTask?: boolean
  onRetry: () => void
}) {
  const output = childTaskOutput(selectedTask)
  const toolEvents = events.filter((event) => !isHiddenRunEvent(event) && ['tool_started', 'tool_call'].includes(event.type || event.event_type || ''))
  return <aside className={`child-agent-panel ${open ? 'is-open' : 'is-closed'}`} aria-label="子 Agent 工作详情" aria-hidden={!open} inert={!open}>
    <header className="child-panel-header"><div><span className="eyebrow">协作执行</span><strong>子 Agent</strong></div><button type="button" className="icon-button" onClick={onClose} aria-label="收起子 Agent 侧栏"><X size={16} /></button></header>
    {!!teammates.length && <section className="teammate-roster" aria-label="持久化队友"><strong>协作队友</strong><div>{teammates.map((teammate) => <span key={teammate.id} className={`teammate teammate-${teammate.status}`} title={teammate.branch_name || teammate.worktree_path || '共享工作区'}><Bot size={12} /><b>{teammate.name}</b><small>{teammate.status}{teammate.workspace_mode === 'worktree' ? ' · worktree' : ''}</small></span>)}</div></section>}
    {error ? <ErrorState message={error} onRetry={onRetry} /> : loading && !tasks.length ? <LoadingState label="正在读取子 Agent…" /> : !tasks.length ? <>
      <EmptyState icon={Users} title="尚未调用子 Agent" description="主 Agent 发起委派后，真实的子 Agent 任务会显示在这里。" />
      {!!durableTasks.length && <PersistentTaskSource
        tasks={durableTasks}
        open={durableSourceOpen}
        selectedTaskId={selectedDurableTaskId}
        onToggle={onToggleDurableSource}
        onSelectTask={onSelectDurableTask}
        onResume={onResumeDurableTask}
        onCancel={onCancelDurableTask}
        cancellingTaskId={cancellingDurableTaskId}
        resuming={resumingDurableTask}
      />}
    </> : <>
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
      <PersistentTaskSource
        tasks={durableTasks}
        branchName={teammates.find((teammate) => teammate.id === selectedTask?.teammate_id)?.branch_name}
        open={durableSourceOpen}
        selectedTaskId={selectedDurableTaskId}
        onToggle={onToggleDurableSource}
        onSelectTask={onSelectDurableTask}
        onResume={onResumeDurableTask}
        onCancel={onCancelDurableTask}
        cancellingTaskId={cancellingDurableTaskId}
        resuming={resumingDurableTask}
      />
      {selectedTask && <section className="child-task-detail">
        <header><div><strong>{selectedTask.title}</strong><small>{childTaskStatusLabel(selectedTask.status)}</small></div><StatusBadge status={selectedTask.status} /></header>
        <dl className="child-task-stats"><div><dt>步骤</dt><dd>{numberFromRecord(selectedTask.result, 'steps') ?? run?.step_count ?? run?.current_step ?? 0}</dd></div><div><dt>工具</dt><dd>{numberFromRecord(selectedTask.result, 'tool_calls') ?? run?.tool_calls ?? 0}</dd></div></dl>
        {selectedTask.description && <section className="child-detail-block"><strong>任务</strong><p>{selectedTask.description}</p></section>}
        {output && <section className="child-detail-block"><strong>{selectedTask.status === 'completed' ? '结果' : '状态说明'}</strong><pre>{output}</pre></section>}
        <section className="child-detail-block child-events"><strong>工作过程</strong>{eventsError ? <p className="inline-error">{eventsError}</p> : eventsLoading ? <p>正在读取运行事件…</p> : toolEvents.length ? <ol>{toolEvents.map((event, index) => <li key={event.id || index}><span>{presentRunEvent(event).title}</span><small>{formatUiDate(event.created_at)}</small></li>)}</ol> : <p>暂未记录工具调用。</p>}</section>
      </section>}
    </>}
  </aside>
})

// MessageBubble 按“身份与时间、附件、正文”的阅读顺序渲染持久消息及其操作。
export const MessageBubble = memo(function MessageBubble({ message, thoughtRunId, thoughtTimeline, onOpenFileChange }: { message: Message; thoughtRunId?: string; thoughtTimeline?: ThoughtTimelineState; onOpenFileChange?: (selection: FileChangeSelection) => void }) {
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
  const finalChangeSet = message.role === 'assistant' ? parseFileChangeSet(message.metadata?.change_summary) : undefined
  const messageRunId = thoughtRunId || (typeof message.metadata?.run_id === 'string' ? message.metadata.run_id : undefined)
  const conversationTurnId = typeof message.turn_id === 'string' && message.turn_id.trim()
    ? message.turn_id.trim()
    : typeof message.metadata?.turn_id === 'string' ? message.metadata.turn_id : ''
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
    <article id={`conversation-message-${message.id}`} data-conversation-turn-id={conversationTurnId || undefined} className={`message ${message.role} ${isTool ? 'tool-message' : ''} ${attachments.length ? 'has-attachments' : ''}`}>
      <div className="message-avatar">{message.role === 'user' ? '你' : isTool ? <SquareTerminal size={16} /> : <PenguinMark size={21} />}</div>
      <div className="message-body">
        <div className="message-meta"><strong>{speaker}</strong><time>{formatUiDate(message.created_at)}</time></div>
        {thoughtRunId && thoughtTimeline && <CompletedThoughtTimeline runId={thoughtRunId} timeline={thoughtTimeline} onOpenFileChange={onOpenFileChange} />}
        {!!imageAttachments.length && <div className="message-image-gallery" aria-label="消息图片">
          {imageAttachments.map((attachment) => {
            const href = apiUrl(`/api/sessions/${message.session_id}/attachments/${attachment.id}/content`)
            return <a key={attachment.id} className="message-image-link" href={href} target="_blank" rel="noreferrer" title={`打开 ${attachment.name}`}>
              <img src={href} alt={attachment.name} loading="lazy" />
            </a>
          })}
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
        {!!message.content && (message.role === 'assistant' || (isTool && message.role !== 'user')
          ? <MarkdownContent content={message.content} />
          : <div className="message-content">{message.content}</div>)}
        {finalChangeSet && <FileChangeActivity
          runId={messageRunId}
          title={`已编辑 ${finalChangeSet.files.length} 个文件`}
          status="completed"
          changeSet={finalChangeSet}
          onOpenFileChange={onOpenFileChange}
        />}
        {!!message.citations?.length && <div className="message-citations" aria-label="参考来源">
          <strong>参考来源</strong>
          {message.citations.map((citation, index) => <a key={`${citation.url}-${index}`} href={citation.url} target="_blank" rel="noreferrer">
            <span>{index + 1}</span>{citation.title || citation.url}
          </a>)}
        </div>}
        {message.status && <StatusBadge status={message.status} />}
        <div className="message-actions">
          <button type="button" className="message-copy-button" aria-label={copied ? '已复制' : '复制消息'} title={copied ? '已复制' : '复制消息'} onClick={() => void copyMessage()}>{copied ? <CheckCheck size={11} /> : <Copy size={11} />}</button>
        </div>
      </div>
    </article>
  )
})

function fileChangeOperationLabel(operation?: string) {
  if (operation === 'add') return '新增'
  if (operation === 'delete') return '删除'
  return '编辑'
}

// FileChangeActivity 只展示用户关心的文件和行数，不暴露底层 apply_patch 工具名。
export function FileChangeActivity({
  runId,
  title,
  status,
  changeSet,
  onOpenFileChange,
}: {
  runId?: string
  title: string
  status: ThoughtActivityItem['status']
  changeSet?: FileChangeSet
  onOpenFileChange?: (selection: FileChangeSelection) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const files = changeSet?.files || []
  const added = changeSet?.added_lines ?? files.reduce((sum, item) => sum + (item.added_lines || 0), 0)
  const deleted = changeSet?.deleted_lines ?? files.reduce((sum, item) => sum + (item.deleted_lines || 0), 0)
  const visibleFiles = expanded ? files : files.slice(0, 3)
  const hiddenCount = Math.max(0, files.length - visibleFiles.length)
  const open = (change: FileChangeRecord) => {
    if (runId && onOpenFileChange) onOpenFileChange({ runId, change })
  }
  return <section className={`file-change-activity ${status}`} aria-label="文件变更">
    <div className="file-change-activity-heading">
      <Pencil size={13} aria-hidden="true" />
      <strong>{title}</strong>
      <span className="file-change-totals" aria-label={`新增 ${added} 行，删除 ${deleted} 行`}><b>+{added}</b><em>−{deleted}</em></span>
    </div>
    {!!files.length && <div className="file-change-activity-list">
      {visibleFiles.map((change) => <button
        type="button"
        className="file-change-activity-row"
        key={`${change.path}:${change.operation}`}
        onClick={() => open(change)}
        disabled={!runId || !onOpenFileChange}
        title={`打开 ${change.path} 的${fileChangeOperationLabel(change.operation)}详情`}
      >
        <span className={`file-change-kind is-${change.operation}`} aria-hidden="true">{change.operation === 'add' ? '+' : change.operation === 'delete' ? '−' : '✎'}</span>
        <span className="file-change-path">{change.path}</span>
        <span className="file-change-line-count"><b>+{change.added_lines || 0}</b><em>−{change.deleted_lines || 0}</em></span>
      </button>)}
      {files.length > 3 && <button
        type="button"
        className={`file-change-activity-more ${expanded ? 'is-expanded' : ''}`}
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span>{expanded ? '收起文件' : `再显示 ${hiddenCount} 个文件`}</span>
        <ChevronDown size={13} aria-hidden="true" />
      </button>}
    </div>}
  </section>
}

// ThoughtActivityIcon 将时间线语义图标映射为具体 Lucide 图形。
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

// 分组工具保持现有紧凑尺寸，但图形仍由每条活动的语义决定，不能统一伪装成终端命令。
function ToolActivityGlyph({ icon, size }: { icon: ThoughtActivityIcon; size: number }) {
  const semanticIcon = icon === 'edit' ? 'write' : icon
  const className = `tool-activity-icon thought-activity-icon is-${semanticIcon}`
  const glyph = icon === 'task' ? <PenguinMark size={size} />
    : icon === 'search' ? <Search size={size} aria-hidden="true" />
      : icon === 'read' ? <FileText size={size} aria-hidden="true" />
        : icon === 'write' || icon === 'edit' ? <Pencil size={size} aria-hidden="true" />
          : icon === 'shell' ? <SquareTerminal size={size} aria-hidden="true" />
            : icon === 'approval' ? <ShieldCheck size={size} aria-hidden="true" />
              : icon === 'context' ? <Globe2 size={size} aria-hidden="true" />
                : icon === 'think' ? <Brain size={size} aria-hidden="true" />
                  : <Wrench size={size} aria-hidden="true" />
  return <span className={className}>{glyph}</span>
}

// ThinkingWaitLine 只在当前模型轮次尚无可见输出时出现；逐字动效不改变可访问名称。
function ThinkingWaitLine({ status, elapsedMs }: { status: string; elapsedMs: number }) {
  const characters = Array.from(status)
  const duration = formatLiveThinkingDuration(elapsedMs)
  return <div className="thought-waiting" aria-label={`${status}，已等待 ${duration}`}>
    <span className="thought-waiting-dot" aria-hidden="true" />
    <span className="thought-waiting-text" aria-hidden="true">
      {characters.map((character, index) => <span key={`${character}-${index}`} style={{ animationDelay: `${(index % 18) * 55}ms` }}>{character === ' ' ? '\u00a0' : character}</span>)}
    </span>
    <time className="thought-waiting-duration" aria-hidden="true">{duration}</time>
  </div>
}

// 同类工具组沿用共同语义；混合工具组使用通用图标，避免标题偏向其中任意一种工具。
function groupedActivityIcon(items: ThoughtActivityItem[]): ThoughtActivityIcon {
  const firstIcon = items[0]?.icon || 'generic'
  return items.every((item) => item.icon === firstIcon) ? firstIcon : 'generic'
}

// 旧运行缺少结构化活动时，从思考文本和工具列表生成兼容展示条目。
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

// 实时和历史阶段只要存在工具、进度或可见思考，就保留执行详情入口；上下文准备事件单独隐藏。
function hasActivityDetails(items: ThoughtActivityItem[]) {
  return items.some((item) => {
    if (item.kind === 'assistant') return item.phase !== 'final_answer' && Boolean(item.detail.trim())
    return item.kind !== 'context' && item.title !== 'Tool Search' && (Boolean(item.detail.trim()) || item.kind !== 'thought')
  })
}

function toolGroupLabel(items: ThoughtActivityItem[]) {
  const shellOnly = items.every((item) => item.icon === 'shell')
  const running = items.some((item) => item.status === 'running')
  const failed = items.some((item) => item.status === 'failed')
  if (shellOnly) return running ? '正在运行命令' : failed ? '运行命令时出错' : '运行了命令'
  return running ? '正在调用多个工具' : failed ? '调用多个工具时出错' : '调用了多个工具'
}

function groupedToolStatus(item: ThoughtActivityItem) {
  if (item.icon === 'edit' || item.title.startsWith('正在编辑') || item.title.startsWith('已编辑') || item.title === '文件编辑失败') {
    return item.title
  }
  if (item.status === 'running') return item.icon === 'shell' ? '正在运行' : '正在调用'
  if (item.status === 'failed') return item.icon === 'shell' ? '运行失败' : '调用失败'
  return item.icon === 'shell' ? '已运行' : '已调用'
}

type OrderedContentEntry =
  | { kind: 'assistant'; item: ThoughtActivityItem }
  | { kind: 'activities'; items: ThoughtActivityItem[] }

// 按 timeline.items 的到达顺序切分内容；只有相邻工具才会在同一个活动块内合并。
function orderedContentEntries(items: ThoughtActivityItem[], includeFinalAssistant: boolean): OrderedContentEntry[] {
  const entries: OrderedContentEntry[] = []
  let activities: ThoughtActivityItem[] = []
  const flushActivities = () => {
    if (activities.length) entries.push({ kind: 'activities', items: activities })
    activities = []
  }
  for (const item of items) {
    if (item.kind === 'assistant') {
      if (item.phase === 'final_answer' && !includeFinalAssistant) continue
      if (!item.detail.trim()) continue
      flushActivities()
      entries.push({ kind: 'assistant', item })
      continue
    }
    activities.push(item)
  }
  flushActivities()
  return entries
}

// 统一渲染模型正文和执行活动，避免“所有活动在前、所有正文在后”的固定布局。
function OrderedRunContent({
  items,
  activitiesVisible,
  runId,
  live,
  activeItemId,
  includeFinalAssistant,
  onOpenFileChange,
}: {
  items: ThoughtActivityItem[]
  activitiesVisible: boolean
  runId?: string
  live?: boolean
  activeItemId?: string
  includeFinalAssistant: boolean
  onOpenFileChange?: (selection: FileChangeSelection) => void
}) {
  return <div className="ordered-run-content">
    {orderedContentEntries(items, includeFinalAssistant).map((entry, index) => {
      if (entry.kind === 'assistant') {
        // 折叠执行详情时保留最终回复；commentary 属于执行过程，随详情一起隐藏。
        if (!activitiesVisible && entry.item.phase !== 'final_answer') return null
        return <div className="ordered-assistant-content" key={entry.item.id}><MarkdownContent content={entry.item.detail} streaming={Boolean(live && entry.item.status === 'running')} /></div>
      }
      return activitiesVisible
        ? <ThoughtActivityList key={`activities-${entry.items[0]?.id || index}`} items={entry.items} runId={runId} live={live} activeItemId={activeItemId} onOpenFileChange={onOpenFileChange} />
        : null
    })}
  </div>
}

type ToolResultDetailState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'loaded'; result: CompleteToolResult }

export function ToolResultDetailPanel({ state, fallbackTitle, onRetry }: { state: ToolResultDetailState; fallbackTitle: string; onRetry: () => void }) {
  if (state.status === 'loading') {
    return <div className="tool-result-detail" aria-busy="true"><span>正在读取完整结果…</span></div>
  }
  if (state.status === 'error') {
    return <div className="tool-result-detail" aria-busy="false"><div className="tool-result-error" role="alert"><span>{state.message}</span><button type="button" onClick={onRetry}>重试</button></div></div>
  }
  const presented = presentToolResult(state.result.content)
  return <div className="tool-result-detail" aria-busy="false">
    <header><strong>完整结果 · {state.result.totalChars} 字符</strong><span>{state.result.toolName || fallbackTitle} · {state.result.ok ? '成功' : '失败'}</span></header>
    {!!presented.status.length && <dl>{presented.status.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>}
    <pre>{presented.content}</pre>
  </div>
}

// CollapsibleRegion 只在首次打开后保留内容，让 CSS 可以完成可打断的收起并保留详情缓存。
function CollapsibleRegion({ expanded, mounted = expanded, children, id, className = '' }: { expanded: boolean; mounted?: boolean; children: ReactNode; id?: string; className?: string }) {
  return <div id={id} className={`collapsible-region ${expanded ? 'is-expanded' : ''} ${className}`.trim()} aria-hidden={!expanded}>
    <div className="collapsible-region-inner">{mounted ? children : null}</div>
  </div>
}

// ThoughtActivityList 统一渲染实时与历史活动；完整结果缓存只跟随当前列表实例的生命周期。
export function ThoughtActivityList({ items, runId, live = false, activeItemId, onOpenFileChange }: { items: ThoughtActivityItem[]; runId?: string; live?: boolean; activeItemId?: string; onOpenFileChange?: (selection: FileChangeSelection) => void }) {
  const [expandedItems, setExpandedItems] = useState<Record<string, boolean>>({})
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({})
  const [mountedItems, setMountedItems] = useState<Record<string, boolean>>({})
  const [mountedGroups, setMountedGroups] = useState<Record<string, boolean>>({})
  const [detailStates, setDetailStates] = useState<Record<string, ToolResultDetailState>>({})
  const [detailLoader] = useState(() => createToolResultDetailLoader())
  function loadDetail(item: ThoughtActivityItem) {
    const request = toolResultRequest(runId, live, item.kind, item.id)
    if (!request || detailStates[item.id]?.status === 'loading' || detailStates[item.id]?.status === 'loaded') return
    setDetailStates((current) => ({ ...current, [item.id]: { status: 'loading' } }))
    void detailLoader.load(request.runId, request.toolCallId).then(
      (result) => setDetailStates((current) => ({ ...current, [item.id]: { status: 'loaded', result } })),
      (error) => setDetailStates((current) => ({ ...current, [item.id]: { status: 'error', message: describeError(error) } })),
    )
  }
  function toggleItem(item: ThoughtActivityItem) {
    const expanding = !expandedItems[item.id]
    if (expanding) setMountedItems((current) => ({ ...current, [item.id]: true }))
    setExpandedItems((current) => ({ ...current, [item.id]: !current[item.id] }))
    if (expanding) loadDetail(item)
  }
  function toggleGroup(id: string) {
    const expanding = !expandedGroups[id]
    if (expanding) setMountedGroups((current) => ({ ...current, [id]: true }))
    setExpandedGroups((current) => ({ ...current, [id]: !current[id] }))
  }
  function openInlineChange(item: ThoughtActivityItem) {
    const change = item.changeSet?.files?.[0]
    if (runId && change && onOpenFileChange) onOpenFileChange({ runId, change })
  }
  const entries = groupThoughtActivities(items)
    .filter((entry) => entry.kind === 'tool-group' || (entry.item.kind !== 'context' && entry.item.title !== 'Tool Search' && (entry.item.kind !== 'thought' || Boolean(entry.item.detail.trim()))))
  if (!entries.length) return null
  return <div className={`thought-activity-list ${live ? 'is-live' : ''}`} aria-label="执行详情">
    {entries.map((entry) => {
      if (entry.kind === 'tool-group') {
        const expanded = Boolean(expandedGroups[entry.id])
        const label = toolGroupLabel(entry.items)
        // 分组工具可以并行处于运行态，不能仅依赖单个 activeItemId 判断是否应展示活动反馈。
        const running = live && entry.items.some((item) => item.status === 'running')
        return <section className={`tool-activity-group ${expanded ? 'expanded' : ''} ${running ? 'is-running' : ''}`} aria-busy={running || undefined} key={entry.id}>
          <button type="button" className="tool-activity-group-toggle" aria-expanded={expanded} onClick={() => toggleGroup(entry.id)}>
            <ToolActivityGlyph icon={groupedActivityIcon(entry.items)} size={14} />
            <span className="tool-activity-group-label">{label}</span>
            {running && <span className="tool-activity-running-indicator" aria-hidden="true"><span /><span /><span /></span>}
            <ChevronRight className="tool-activity-group-chevron" size={14} aria-hidden="true" />
          </button>
          <CollapsibleRegion expanded={expanded} mounted={Boolean(mountedGroups[entry.id]) || expanded} className="tool-activity-group-collapse">
            <div className="tool-activity-group-items">
            {entry.items.map((item) => {
              const itemExpanded = Boolean(expandedItems[item.id])
              const detailRequest = toolResultRequest(runId, live, item.kind, item.id)
              const expandable = !item.changeSet && (Boolean(item.detail.trim()) || Boolean(detailRequest))
              const itemRunning = live && item.status === 'running'
              return <div className={`tool-activity-group-item ${item.status} ${itemRunning ? 'is-running' : ''}`} key={item.id}>
                {item.changeSet?.files?.[0] && runId && onOpenFileChange ? <button type="button" className="tool-activity-group-item-line is-clickable" onClick={() => openInlineChange(item)}>
                  <ToolActivityGlyph icon={item.icon} size={13} />
                  <span><strong>{groupedToolStatus(item)}</strong>{item.detail && <code>{item.detail}</code>}</span>
                </button> : expandable ? <button type="button" className="tool-activity-group-item-toggle" aria-expanded={itemExpanded} onClick={() => toggleItem(item)}>
                  <ToolActivityGlyph icon={item.icon} size={13} />
                  <span><strong>{groupedToolStatus(item)}</strong>{item.detail && <code>{item.detail}</code>}</span>
                  <ChevronRight className="tool-activity-item-chevron" size={13} aria-hidden="true" />
                </button> : <div className="tool-activity-group-item-line">
                  <ToolActivityGlyph icon={item.icon} size={13} />
                  <span><strong>{groupedToolStatus(item)}</strong>{item.detail && <code>{item.detail}</code>}</span>
                </div>}
                {expandable && <CollapsibleRegion expanded={itemExpanded} mounted={Boolean(mountedItems[item.id]) || itemExpanded} className="tool-activity-item-collapse">
                  {detailRequest
                    ? <ToolResultDetailPanel state={detailStates[item.id] || { status: 'loading' }} fallbackTitle={item.title} onRetry={() => loadDetail(item)} />
                    : <div className="tool-activity-group-detail"><strong>{item.title}</strong><pre>{item.detail}</pre></div>}
                </CollapsibleRegion>}
              </div>
            })}
            </div>
          </CollapsibleRegion>
        </section>
      }
      const item = entry.item
      if (item.kind === 'assistant') return <div key={item.id} className="ordered-assistant-content"><MarkdownContent content={item.detail} streaming={Boolean(live && item.status === 'running')} /></div>
      if (item.kind === 'file-change') return <FileChangeActivity key={item.id} runId={runId} title={item.title} status={item.status} changeSet={item.changeSet} onOpenFileChange={onOpenFileChange} />
      if (item.kind === 'thought') return <p key={item.id} className={`thought-activity-thought ${item.status}`}>{item.detail}</p>
      const detailRequest = toolResultRequest(runId, live, item.kind, item.id)
      const hasLongUrl = /https?:\/\/\S{72,}/i.test(item.detail)
      const expandable = item.kind === 'tool' && !item.changeSet && (Boolean(detailRequest) || (Boolean(item.detail.trim()) && (item.title !== '联网搜索' || hasLongUrl)))
      const expanded = Boolean(expandedItems[item.id])
      return <div key={item.id} className={`thought-activity kind-${item.kind} ${item.status} ${live && item.status === 'running' && item.id === activeItemId ? 'is-active' : ''}`}>
        <ThoughtActivityIcon icon={item.icon} />
        <div className="thought-activity-copy">
          {item.changeSet?.files?.[0] && runId && onOpenFileChange ? <button type="button" className="thought-activity-static is-clickable" onClick={() => openInlineChange(item)}><span className="thought-activity-title">{item.title}</span>{item.detail && item.title !== '准备上下文' && <span className="thought-activity-summary">{item.detail}</span>}</button> : expandable ? <>
            <button type="button" className="thought-activity-toggle" aria-expanded={expanded} onClick={() => toggleItem(item)}><span><span className="thought-activity-title">{item.title}</span><span className="thought-activity-summary">{item.detail}</span></span><ChevronRight className="thought-activity-chevron" size={14} aria-hidden="true" /></button>
            <CollapsibleRegion expanded={expanded} mounted={Boolean(mountedItems[item.id]) || expanded} className="thought-activity-detail-collapse">
              {detailRequest
              ? <ToolResultDetailPanel state={detailStates[item.id] || { status: 'loading' }} fallbackTitle={item.title} onRetry={() => loadDetail(item)} />
              : <div className="thought-activity-detail">{item.detail}</div>}
            </CollapsibleRegion>
          </> : <div className="thought-activity-static"><span className="thought-activity-title">{item.title}</span>{item.detail && item.title !== '准备上下文' && <span className="thought-activity-summary">{item.detail}</span>}</div>}
        </div>
      </div>
    })}
  </div>
}

// CompletedThoughtTimeline 在助手历史消息上方展示可折叠的执行详情。
export const CompletedThoughtTimeline = memo(function CompletedThoughtTimeline({ runId, timeline, onOpenFileChange }: { runId: string; timeline: ThoughtTimelineState; onOpenFileChange?: (selection: FileChangeSelection) => void }) {
  const [expanded, setExpanded] = useState(false)
  const [mounted, setMounted] = useState(false)
  const duration = formatThoughtDuration(timeline.elapsedMs)
  const items = fallbackThoughtItems(timeline)
  const hasDetails = hasActivityDetails(items)
  const summary = timeline.conclusion || '执行完成'
  const toggleExpanded = () => {
    if (!expanded) setMounted(true)
    setExpanded((value) => !value)
  }

  return <article className={`completed-thought ${hasDetails && expanded ? 'expanded' : ''} ${hasDetails ? '' : 'no-details'}`}>
    {hasDetails ? <button type="button" className="completed-thought-toggle" aria-expanded={expanded} aria-controls={`thought-details-${runId}`} onClick={toggleExpanded}>
      <span className="completed-thought-duration">执行详情 · 用时 {duration}</span><span className="completed-thought-summary">{summary}</span><ChevronRight className="completed-thought-chevron" size={13} aria-hidden="true" />
    </button> : <span className="completed-thought-duration completed-thought-static">用时 {duration}</span>}
    {hasDetails && <CollapsibleRegion id={`thought-details-${runId}`} expanded={expanded} mounted={mounted || expanded} className="completed-thought-collapse"><OrderedRunContent items={items} activitiesVisible runId={runId} includeFinalAssistant={false} onOpenFileChange={onOpenFileChange} /></CollapsibleRegion>}
  </article>
})

// 每个运行阶段以独立实例持有计时和展开状态：新运行默认展开，进入终态时默认收起且仍允许用户再次展开。
function LiveAssistantMessageState({ liveRun, initiallyExpanded, onOpenFileChange }: { liveRun: LiveRunView; initiallyExpanded: boolean; onOpenFileChange?: (selection: FileChangeSelection) => void }) {
  const [now, setNow] = useState(() => Date.now())
  const [expanded, setExpanded] = useState(initiallyExpanded)
  const [mounted, setMounted] = useState(initiallyExpanded)
  useEffect(() => {
    if (liveRun.thought.startedAt === null || liveRun.thought.finished) return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [liveRun.thought.finished, liveRun.thought.startedAt])
  const phase = liveRun.phase || (liveRun.thought.finished ? '已完成' : '思考中…')
  const items = fallbackThoughtItems(liveRun.thought)
  const assistantItems = liveRun.assistantItems?.length
    ? liveRun.assistantItems
    : liveRun.draft
      ? [{ id: 'legacy-live-output', content: liveRun.draft, status: 'streaming' as const, phase: 'final_answer' }]
      : []
  const displayItems = items.some((item) => item.kind === 'assistant')
    ? items
    : [
      ...items,
      ...assistantItems.map((item) => ({
        id: item.id,
        kind: 'assistant' as const,
        icon: 'think' as const,
        title: item.phase === 'final_answer' ? '最终回复' : '中间回复',
        detail: item.content,
        phase: item.phase || 'unknown',
        status: item.status === 'completed' ? 'completed' as const : 'running' as const,
      })),
    ]
  // final_answer 是用户最终看到的正文，不属于可折叠的执行详情；其余 assistant 条目才是 commentary。
  const executionItems = displayItems.filter((item) => item.kind !== 'assistant' || item.phase !== 'final_answer')
  const finalItems = displayItems.filter((item) => item.kind === 'assistant' && item.phase === 'final_answer')
  const hasDetails = hasActivityDetails(executionItems)
  const activeStepStartedAt = liveRun.thought.activeStepStartedAt ?? liveRun.thought.startedAt
  // 缺少轮次元数据时不能把旧工具详情当作当前轮输出，否则模型长时间无可见事件会静默。
  const activeStepHasVisibleContent = liveRun.thought.activeStepHasVisibleContent ?? false
  const waitingStatus = liveRun.thinkingStatus || '思考中…'
  const waitingForVisibleContent = !liveRun.thought.finished
    && liveRun.status !== 'awaiting_approval'
    && activeStepStartedAt !== null
    && !activeStepHasVisibleContent
  const activeStepElapsedMs = activeStepStartedAt === null ? 0 : Math.max(0, now - activeStepStartedAt)
  const showExecutionDetails = hasDetails || waitingForVisibleContent
  const toggleExpanded = () => {
    if (!expanded) setMounted(true)
    setExpanded((value) => !value)
  }
  return (
    <article className={`message assistant live-message ${liveRun.status === 'terminal' ? 'live-message-terminal' : ''}`}>
      <div className="message-avatar"><PenguinMark size={21} /></div>
      <div className="message-body"><div className="message-meta"><strong>PGAgent</strong><span className="live-phase">{phase}</span></div>
        {showExecutionDetails
          ? <div className={`live-thought ${expanded ? 'expanded' : ''}`}>
            <button type="button" className="live-thought-toggle" aria-expanded={expanded} onClick={toggleExpanded}><ChevronRight className="live-thought-chevron" size={13} aria-hidden="true" /><span>{liveRun.thought.finished ? `执行详情 · 用时 ${formatThoughtDuration(liveRun.thought.elapsedMs)}` : '执行详情'}</span></button>
            <CollapsibleRegion expanded={expanded} mounted={mounted || expanded} className="live-thought-collapse">
              <OrderedRunContent items={executionItems} activitiesVisible runId={liveRun.runId} live activeItemId={liveRun.thought.activeItemId} includeFinalAssistant={false} onOpenFileChange={onOpenFileChange} />
              {waitingForVisibleContent && <ThinkingWaitLine status={waitingStatus} elapsedMs={activeStepElapsedMs} />}
            </CollapsibleRegion>
          </div>
          : null}
        {!!liveRun.plan?.length && <RunPlanProgress plan={liveRun.plan} />}
        {!!finalItems.length && <div className="live-final-content"><OrderedRunContent items={finalItems} activitiesVisible runId={liveRun.runId} live includeFinalAssistant /></div>}
        {liveRun.error && <p className="live-error">{liveRun.error}</p>}
      </div>
    </article>
  )
}

// LiveAssistantMessage 合并阶段、实时耗时、活动时间线和逐字回复草稿。
export const LiveAssistantMessage = memo(function LiveAssistantMessage({ liveRun, onOpenFileChange }: { liveRun: LiveRunView; onOpenFileChange?: (selection: FileChangeSelection) => void }) {
  const terminal = liveRun.thought.finished || liveRun.status === 'terminal'
  const stateKey = `${liveRun.runId}:${liveRun.thought.startedAt ?? 'pending'}:${terminal ? 'terminal' : 'active'}`
  return <LiveAssistantMessageState key={stateKey} liveRun={liveRun} initiallyExpanded={!terminal} onOpenFileChange={onOpenFileChange} />
})

// ApprovalCard 展示待执行动作及风险信息，并将批准/拒绝决策回传会话协调器。
export const ApprovalCard = memo(function ApprovalCard({ approval, deciding, onDecision, embedded = false }: { approval: Approval; deciding: boolean; onDecision: (id: string, decision: 'approve' | 'reject', runId: string) => void; embedded?: boolean }) {
  const runId = typeof approval.run_id === 'string' || typeof approval.run_id === 'number' ? String(approval.run_id) : ''
  const formattedArguments = formatApprovalArguments(approval.tool_name, approval.arguments)
  return (
    <article className={`approval-card${embedded ? ' approval-card-embedded' : ''}`}>
      <header><span><ShieldCheck size={17} /></span><div><strong>需要你的批准</strong><p>Agent 请求执行有副作用的工具</p></div><StatusBadge status={approval.status || 'pending'} /></header>
      <div className="approval-command"><span>{approval.tool_name || 'unknown_tool'}</span><pre>{formattedArguments}</pre></div>
      {approval.reason && <p className="approval-reason">理由：{approval.reason}</p>}
      <footer><button className="button button-danger" disabled={deciding || !runId} onClick={() => onDecision(approval.id, 'reject', runId)}><XCircle size={15} />拒绝</button><button className="button button-primary" disabled={deciding || !runId} onClick={() => onDecision(approval.id, 'approve', runId)}>{deciding ? <LoaderCircle className="spin" size={15} /> : <CheckCircle2 size={15} />}允许本次</button></footer>
    </article>
  )
})
