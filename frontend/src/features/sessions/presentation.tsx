// 本文件实现 presentation 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
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
import { presentRunEvent } from '../../runEventPresentation'
import { MarkdownContent } from './MarkdownContent'
import { groupThoughtActivities } from './thoughtActivityGrouping'
import { formatApprovalArguments } from './approvalPresentation'

// LiveRunView 是运输状态到实时回复组件之间的最小只读接口。
export type LiveRunView = {
  runId: string
  phase: string
  draft: string
  assistantItems?: Array<{ id: string; content: string; status: 'streaming' | 'completed'; responseId?: string; itemId?: string; outputIndex?: number; phase?: string }>
  status: 'idle' | 'connecting' | 'live' | 'fallback' | 'awaiting_approval' | 'terminal'
  error: string
  thought: ThoughtTimelineState
  thinkingStatus: string
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
        <section className="child-detail-block child-events"><strong>工作过程</strong>{eventsError ? <p className="inline-error">{eventsError}</p> : eventsLoading ? <p>正在读取运行事件…</p> : toolEvents.length ? <ol>{toolEvents.map((event, index) => <li key={event.id || index}><span>{presentRunEvent(event).title}</span><small>{formatUiDate(event.created_at)}</small></li>)}</ol> : <p>暂未记录工具调用。</p>}</section>
      </section>}
    </>}
  </aside>
})

// MessageBubble 根据角色渲染正文、附件和复制动作，是持久消息的统一展示入口。
export const MessageBubble = memo(function MessageBubble({ message, thoughtRunId, thoughtTimeline }: { message: Message; thoughtRunId?: string; thoughtTimeline?: ThoughtTimelineState }) {
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
        {thoughtRunId && thoughtTimeline && <CompletedThoughtTimeline runId={thoughtRunId} timeline={thoughtTimeline} />}
        {!!imageAttachments.length && <div className="message-image-gallery" aria-label="消息图片">
          {imageAttachments.map((attachment) => {
            const href = apiUrl(`/api/sessions/${message.session_id}/attachments/${attachment.id}/content`)
            return <a key={attachment.id} className="message-image-link" href={href} target="_blank" rel="noreferrer" title={`打开 ${attachment.name}`}>
              <img src={href} alt={attachment.name} loading="lazy" />
            </a>
          })}
        </div>}
        {!!message.content && (message.role === 'assistant' || (isTool && message.role !== 'user')
          ? <MarkdownContent content={message.content} />
          : <div className="message-content">{message.content}</div>)}
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
  live,
  activeItemId,
  includeFinalAssistant,
}: {
  items: ThoughtActivityItem[]
  activitiesVisible: boolean
  live?: boolean
  activeItemId?: string
  includeFinalAssistant: boolean
}) {
  return <div className="ordered-run-content">
    {orderedContentEntries(items, includeFinalAssistant).map((entry, index) => {
      if (entry.kind === 'assistant') {
        // 折叠执行详情时保留最终回复；commentary 属于执行过程，随详情一起隐藏。
        if (!activitiesVisible && entry.item.phase !== 'final_answer') return null
        return <div className="ordered-assistant-content" key={entry.item.id}><MarkdownContent content={entry.item.detail} /></div>
      }
      return activitiesVisible
        ? <ThoughtActivityList key={`activities-${entry.items[0]?.id || index}`} items={entry.items} live={live} activeItemId={activeItemId} />
        : null
    })}
  </div>
}

// ThoughtActivityList 统一渲染实时与历史活动，并突出当前活动。
function ThoughtActivityList({ items, live = false, activeItemId }: { items: ThoughtActivityItem[]; live?: boolean; activeItemId?: string }) {
  const [expandedItems, setExpandedItems] = useState<Record<string, boolean>>({})
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({})
  const entries = groupThoughtActivities(items)
    .filter((entry) => entry.kind === 'tool-group' || (entry.item.kind !== 'context' && entry.item.title !== 'Tool Search' && (entry.item.kind !== 'thought' || Boolean(entry.item.detail.trim()))))
  if (!entries.length) return null
  return <div className={`thought-activity-list ${live ? 'is-live' : ''}`} aria-label="执行详情">
    {entries.map((entry) => {
      if (entry.kind === 'tool-group') {
        const expanded = Boolean(expandedGroups[entry.id])
        const label = toolGroupLabel(entry.items)
        return <section className={`tool-activity-group ${expanded ? 'expanded' : ''}`} key={entry.id}>
          <button type="button" className="tool-activity-group-toggle" aria-expanded={expanded} onClick={() => setExpandedGroups((current) => ({ ...current, [entry.id]: !current[entry.id] }))}>
            <SquareTerminal size={14} aria-hidden="true" />
            <span>{label}</span>
            <ChevronRight className="tool-activity-group-chevron" size={14} aria-hidden="true" />
          </button>
          {expanded && <div className="tool-activity-group-items">
            {entry.items.map((item) => {
              const itemExpanded = Boolean(expandedItems[item.id])
              const expandable = Boolean(item.detail.trim())
              return <div className={`tool-activity-group-item ${item.status}`} key={item.id}>
                <button type="button" className="tool-activity-group-item-toggle" aria-expanded={expandable ? itemExpanded : undefined} disabled={!expandable} onClick={() => expandable && setExpandedItems((current) => ({ ...current, [item.id]: !current[item.id] }))}>
                  <SquareTerminal size={13} aria-hidden="true" />
                  <span><strong>{groupedToolStatus(item)}</strong>{item.detail && <code>{item.detail}</code>}</span>
                  {expandable && <ChevronRight className="tool-activity-item-chevron" size={13} aria-hidden="true" />}
                </button>
                {itemExpanded && <div className="tool-activity-group-detail"><strong>{item.title}</strong><pre>{item.detail}</pre></div>}
              </div>
            })}
          </div>}
        </section>
      }
      const item = entry.item
      if (item.kind === 'assistant') return <div key={item.id} className="ordered-assistant-content"><MarkdownContent content={item.detail} /></div>
      if (item.kind === 'thought') return <p key={item.id} className={`thought-activity-thought ${item.status}`}>{item.detail}</p>
      // 普通联网搜索保持紧凑；当来源 URL 过长时提供展开入口，避免摘要撑坏标题布局。
      const hasLongUrl = /https?:\/\/\S{72,}/i.test(item.detail)
      const expandable = item.kind === 'tool' && Boolean(item.detail.trim()) && (item.title !== '联网搜索' || hasLongUrl)
      const expanded = Boolean(expandedItems[item.id])
      return <div key={item.id} className={`thought-activity kind-${item.kind} ${item.status} ${live && item.status === 'running' && item.id === activeItemId ? 'is-active' : ''}`}>
        <ThoughtActivityIcon icon={item.icon} />
        <div className="thought-activity-copy">
          {expandable ? <>
            <button type="button" className="thought-activity-toggle" aria-expanded={expanded} onClick={() => setExpandedItems((current) => ({ ...current, [item.id]: !current[item.id] }))}><span><span className="thought-activity-title">{item.title}</span><span className="thought-activity-summary">{item.detail}</span></span><ChevronRight className="thought-activity-chevron" size={14} aria-hidden="true" /></button>
            {expanded && <div className="thought-activity-detail">{item.detail}</div>}
          </> : <div className="thought-activity-static"><span className="thought-activity-title">{item.title}</span>{item.detail && item.title !== '准备上下文' && <span className="thought-activity-summary">{item.detail}</span>}</div>}
        </div>
      </div>
    })}
  </div>
}

// CompletedThoughtTimeline 在助手历史消息上方展示可折叠的执行详情。
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
    {hasDetails && <div id={`thought-details-${runId}`}><OrderedRunContent items={items} activitiesVisible={expanded} includeFinalAssistant={false} /></div>}
  </article>
})

// 每个运行阶段以独立实例持有计时和展开状态：新运行默认展开，进入终态时默认收起且仍允许用户再次展开。
function LiveAssistantMessageState({ liveRun, initiallyExpanded }: { liveRun: LiveRunView; initiallyExpanded: boolean }) {
  const [now, setNow] = useState(() => Date.now())
  const [expanded, setExpanded] = useState(initiallyExpanded)
  useEffect(() => {
    if (liveRun.thought.startedAt === null || liveRun.thought.finished) return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [liveRun.thought.finished, liveRun.thought.startedAt])
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
  return (
    <article className={`message assistant live-message ${liveRun.status === 'terminal' ? 'live-message-terminal' : ''}`}>
      <div className="message-avatar"><PenguinMark size={21} /></div>
      <div className="message-body"><div className="message-meta"><strong>PGAgent</strong><span className="live-phase">{phase}</span></div>
        {hasDetails
          ? <div className={`live-thought ${expanded ? 'expanded' : ''}`}>
            <button type="button" className="live-thought-toggle" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}><ChevronRight className="live-thought-chevron" size={13} aria-hidden="true" /><span>{liveRun.thought.finished ? `执行详情 · 用时 ${formatThoughtDuration(liveRun.thought.elapsedMs)}` : '执行详情'}</span></button>
            <OrderedRunContent items={executionItems} activitiesVisible={expanded} live activeItemId={liveRun.thought.activeItemId} includeFinalAssistant={false} />
          </div>
          : null}
        {!!finalItems.length && <div className="live-final-content"><OrderedRunContent items={finalItems} activitiesVisible includeFinalAssistant /></div>}
        {liveRun.error && <p className="live-error">{liveRun.error}</p>}
      </div>
    </article>
  )
}

// LiveAssistantMessage 合并阶段、实时耗时、活动时间线和逐字回复草稿。
export const LiveAssistantMessage = memo(function LiveAssistantMessage({ liveRun }: { liveRun: LiveRunView }) {
  const terminal = liveRun.thought.finished || liveRun.status === 'terminal'
  const stateKey = `${liveRun.runId}:${liveRun.thought.startedAt ?? 'pending'}:${terminal ? 'terminal' : 'active'}`
  return <LiveAssistantMessageState key={stateKey} liveRun={liveRun} initiallyExpanded={!terminal} />
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
