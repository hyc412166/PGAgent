import { useContext, useEffect } from 'react'
import { api } from '../../../api'
import type { DelegatedTask, Run, RunEvent } from '../../../types'
import { useApiData } from '../../../shared/hooks/useApiData'
import { ErrorState, LoadingState, StatusBadge } from '../../../components/ui'
import { timelineFromRunEvents } from '../../../thoughtTimeline'
import { CompletedThoughtTimeline } from '../presentation'
import { MarkdownContent } from '../MarkdownContent'
import { childReplies } from '../childReplies'
import { ChildNavigation } from '../childNavigation'

type CollaborationMessage = { id: string; sender_worker_id: string | null; recipient_worker_id: string | null; content: string; created_at: string }

export function ChildConversation({ task }: { task: DelegatedTask }) {
  const navigation = useContext(ChildNavigation)
  const runId = task.child_run_id || String(task.result?.child_run_id || '')
  const data = useApiData<{ events: RunEvent[]; run?: Run; messages: CollaborationMessage[]; limited?: boolean }>({ events: [], messages: [] }, async () => {
    // 历史事件按游标读完，不能把默认第一页当成完整子会话。
    const readEvents = async () => {
      const events: RunEvent[] = []
      let before: number | undefined
      do {
        const page = await api.listRunEvents(runId, { before, limit: 200 })
        events.push(...page.items)
        before = page.next_before ?? undefined
      } while (before !== undefined)
      return events
    }
    const [events, run, messages] = await Promise.all([
      runId ? readEvents() : Promise.resolve([]),
      runId ? api.get<Run>(`/api/runs/${encodeURIComponent(runId)}`) : Promise.resolve(undefined),
      task.teammate_id && task.parent_session_id ? api.list<CollaborationMessage>(`/api/sessions/${encodeURIComponent(task.parent_session_id)}/collaboration-messages?limit=500`) : Promise.resolve([]),
    ])
    // 协作邮箱属于队友而非单次委派；仅显示本次委派时间范围内的主子通信。
    return { events, run, limited: messages.length === 500, messages: messages.filter(m =>
      ((m.sender_worker_id === task.teammate_id && m.recipient_worker_id === null) || (m.recipient_worker_id === task.teammate_id && m.sender_worker_id === null))
      && (!task.created_at || m.created_at >= task.created_at)
      && (!run?.finished_at || m.created_at <= run.finished_at)
    ).reverse() }
  }, [runId, task.teammate_id, task.parent_session_id, task.status])
  const { reload } = data
  const terminal = ['completed', 'failed', 'stopped', 'cancelled'].includes(data.data.run?.status || task.status || '')
  useEffect(() => {
    if (terminal) return
    let stopped = false
    let timer: number
    const poll = async () => {
      await reload()
      if (!stopped) timer = window.setTimeout(poll, 2000)
    }
    timer = window.setTimeout(poll, 2000)
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [terminal, reload])
  const name = navigation?.names[task.id] || data.data.run?.agent_name || task.child_agent_name || '子 Agent'
  const replies = childReplies(data.data.events)
  return <section className="child-conversation messages" aria-label="主 Agent 与子 Agent 的会话">
    <article className="message user"><div className="message-body"><div className="message-meta"><strong>主 Agent</strong></div><div className="message-content">{task.description || task.title}</div></div></article>
    {data.initialLoading && <LoadingState label="正在读取子 Agent 会话…" />}
    {data.error && <ErrorState message={data.error} onRetry={reload} />}
    <article className="message assistant"><div className="message-body"><div className="message-meta"><strong>{name}</strong><StatusBadge status={data.data.run?.status || task.status} /></div>
      {!!data.data.events.length && <CompletedThoughtTimeline runId={runId} timeline={timelineFromRunEvents(data.data.events.filter(event => !(event.event_type || event.type || '').startsWith('assistant_message_')).map(event => ({ ...event, type: event.event_type || event.type || '' })))} />}
      {replies.map(reply => <MarkdownContent key={reply.id} content={reply.content} />)}
      {!data.loading && !data.error && !replies.length && <p className="inline-notice">{terminal ? '此运行没有已记录的回复正文。' : '子 Agent 正在执行任务…'}</p>}
    </div></article>
    {data.data.limited && <p role="status">协作消息仅包含会话最近 500 条中的相关记录，较早消息可能未显示。</p>}
    {!!data.data.messages.length && <h3>协作通信记录</h3>}
    {data.data.messages.map(message => <article key={message.id} className={`message ${message.sender_worker_id === task.teammate_id ? 'assistant' : 'user'}`}><div className="message-body"><div className="message-meta"><strong>{message.sender_worker_id === task.teammate_id ? name : '主 Agent'}</strong><time>{message.created_at}</time></div><MarkdownContent content={message.content} /></div></article>)}
  </section>
}
