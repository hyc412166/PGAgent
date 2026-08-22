import {
  AlertCircle,
  Box,
  Check,
  LoaderCircle,
  RefreshCw,
  X,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useState, type ReactNode, type TransitionEvent } from 'react'
import { buildContextUsageView } from '../contextUsage'
import type { SessionContext } from '../types'
import { PenguinMark } from './penguin'
import { statusText } from './status'

export function StatusBadge({ status = 'unknown' }: { status?: string }) {
  const normalized = status.toLowerCase()
  const tone = ['completed', 'complete', 'approved', 'online', 'healthy', 'active'].includes(normalized)
    ? 'success'
    : ['failed', 'rejected', 'blocked', 'stopped'].includes(normalized)
      ? 'danger'
      : ['running', 'acting', 'planning', 'preparing_context', 'verifying', 'in_progress'].includes(normalized)
        ? 'active'
        : ['awaiting_approval', 'pending', 'review'].includes(normalized)
          ? 'warning'
          : 'neutral'
  return <span className={`status status-${tone}`}><i aria-hidden="true" />{statusText[normalized] ?? status}</span>
}

export function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: ReactNode }) {
  return (
    <header className="page-header">
      <div className="page-heading-copy">
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="page-description">{description}</p>
      </div>
      <div className="page-header-side">
        <span className="page-mascot" aria-hidden="true"><PenguinMark size={46} /></span>
        {action && <div className="page-action">{action}</div>}
      </div>
    </header>
  )
}

export function EmptyState({ icon: Icon = Box, title, description, action }: { icon?: LucideIcon; title: string; description: string; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-icon"><Icon size={22} /></div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  )
}

export function LoadingState({ label = '正在读取本地数据' }: { label?: string }) {
  return <div className="loading-state"><LoaderCircle className="spin" size={18} />{label}</div>
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-state" role="alert">
      <AlertCircle size={19} />
      <div><strong>数据暂时不可用</strong><p>{message}</p></div>
      {onRetry && <button className="button button-quiet" onClick={onRetry}><RefreshCw size={15} />重试</button>}
    </div>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>
}

export function CapabilityMultiSelect({
  label,
  items,
  selectedIds,
  loading,
  error,
  onRetry,
  onToggle,
}: {
  label: string
  items: Array<{ id: string; name: string; description?: string; enabled?: boolean }>
  selectedIds: string[]
  loading: boolean
  error: string
  onRetry: () => void
  onToggle: (id: string) => void
}) {
  return <section className="capability-select" aria-label={label}>
    <header><strong>{label}</strong><span>已选择 {selectedIds.length}</span></header>
    {error ? <div className="capability-inline-state error"><AlertCircle size={13} /><span>{error}</span><button type="button" onClick={onRetry}>重试</button></div>
      : loading ? <div className="capability-inline-state"><LoaderCircle className="spin" size={13} />正在读取…</div>
        : items.length ? <div className="capability-options">{items.map((item) => {
          const selected = selectedIds.includes(item.id)
          return <button key={item.id} type="button" className={selected ? 'selected' : ''} aria-pressed={selected} disabled={item.enabled === false} onClick={() => onToggle(item.id)}>
            <span className="capability-check">{selected && <Check size={12} />}</span>
            <span><strong>{item.name}</strong>{item.description && <small>{item.description}</small>}</span>
          </button>
        })}</div>
          : <p className="capability-empty">目录中暂无可用项目。</p>}
  </section>
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

export function ContextUsageRing({ context }: { context: SessionContext }) {
  const view = buildContextUsageView(context)
  return (
    <div className={`context-usage context-${view.tone}`} tabIndex={0} role="img" aria-label={view.ariaLabel} aria-describedby="context-usage-tooltip">
      <span className="context-ring" aria-hidden="true">
        <svg viewBox="0 0 24 24">
          <circle className="context-ring-track" cx="12" cy="12" r="9" pathLength="100" />
          <circle className="context-ring-value" cx="12" cy="12" r="9" pathLength="100" strokeDasharray="100" strokeDashoffset={100 - view.percent} />
        </svg>
      </span>
      <div className="context-tooltip" id="context-usage-tooltip" role="tooltip">
        <strong>{view.roundedPercent}% 已用</strong>
        <p>{view.detail}</p>
        <small>{view.compressionHint}</small>
        {context.last_compaction_at && <small>上次压缩：{formatUiDate(context.last_compaction_at)}</small>}
      </div>
    </div>
  )
}

export function SlidePanel({ title, description, open = true, onClose, onExited, children }: { title: string; description?: string; open?: boolean; onClose: () => void; onExited?: () => void; children: ReactNode }) {
  const [rendered, setRendered] = useState(open)

  useEffect(() => {
    if (open) setRendered(true)
  }, [open])

  useEffect(() => {
    if (!open) return
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose, open])

  if (!rendered) return null

  function finishClosing(event: TransitionEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement
    if (open || event.propertyName !== 'transform' || !target.classList.contains('slide-panel')) return
    setRendered(false)
    onExited?.()
  }

  return (
    <div className={`overlay motion-panel ${open ? 'is-open' : 'is-closed'}`} role="presentation" aria-hidden={!open} onTransitionEnd={finishClosing} onMouseDown={(event) => open && event.target === event.currentTarget && onClose()}>
      <section className="slide-panel" role="dialog" aria-modal="true" aria-labelledby="panel-title">
        <header>
          <div><p className="eyebrow">企鹅配置舱</p><h2 id="panel-title">{title}</h2>{description && <p>{description}</p>}</div>
          <button className="icon-button" onClick={onClose} aria-label="关闭"><X size={19} /></button>
        </header>
        {children}
      </section>
    </div>
  )
}
