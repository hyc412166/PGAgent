import { ChevronLeft, ChevronRight, FileCode2, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api, describeError } from '../../../api'
import type { FileChangeSelection, FileContentRead } from '../../../types'

type DiffRow = {
  kind: 'hunk' | 'context' | 'add' | 'delete' | 'meta'
  text: string
  oldLine?: number
  newLine?: number
  hunk?: number
}

function parseDiff(diff: string): DiffRow[] {
  const rows: DiffRow[] = []
  let oldLine = 0
  let newLine = 0
  let hunk = -1
  for (const raw of diff.replace(/\r\n?/g, '\n').split('\n')) {
    const header = raw.match(/^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/)
    if (header) {
      hunk += 1
      oldLine = Number(header[1])
      newLine = Number(header[2])
      rows.push({ kind: 'hunk', text: raw, hunk })
      continue
    }
    if (raw.startsWith('diff --git') || raw.startsWith('index ') || raw.startsWith('--- ') || raw.startsWith('+++ ')) {
      rows.push({ kind: 'meta', text: raw })
      continue
    }
    if (raw.startsWith('+')) {
      rows.push({ kind: 'add', text: raw.slice(1), newLine, hunk: Math.max(0, hunk) })
      newLine += 1
      continue
    }
    if (raw.startsWith('-')) {
      rows.push({ kind: 'delete', text: raw.slice(1), oldLine, hunk: Math.max(0, hunk) })
      oldLine += 1
      continue
    }
    if (raw.startsWith(' ')) {
      rows.push({ kind: 'context', text: raw.slice(1), oldLine, newLine, hunk: Math.max(0, hunk) })
      oldLine += 1
      newLine += 1
      continue
    }
    if (raw) rows.push({ kind: 'meta', text: raw, hunk: Math.max(0, hunk) })
  }
  return rows
}

function operationLabel(operation?: string) {
  if (operation === 'add') return '新增'
  if (operation === 'delete') return '删除'
  return '编辑'
}

function lineNumber(value?: number) {
  return value === undefined ? '' : String(value)
}

function currentFileLines(content: string) {
  const lines = content.replace(/\r\n?/g, '\n').split('\n')
  if (lines.at(-1) === '') lines.pop()
  return lines
}

export function FileChangePanel({ selection, onClose }: { selection: FileChangeSelection; onClose: () => void }) {
  const { runId, change } = selection
  const [mode, setMode] = useState<'diff' | 'file'>('diff')
  const [activeHunk, setActiveHunk] = useState(0)
  const [fileState, setFileState] = useState<{ status: 'loading' | 'loaded' | 'error'; value?: FileContentRead; error?: string }>({ status: 'loading' })
  const hunkRefs = useRef<Record<number, HTMLDivElement | null>>({})
  const rows = useMemo(() => parseDiff(change.diff || ''), [change.diff])
  const hunkIndexes = useMemo(() => rows.filter((row) => row.kind === 'hunk').map((row) => row.hunk ?? 0), [rows])

  useEffect(() => {
    setMode('diff')
    setActiveHunk(0)
    hunkRefs.current = {}
    let cancelled = false
    setFileState({ status: 'loading' })
    void api.getRunFileContent(runId, change.path).then(
      (value) => { if (!cancelled) setFileState({ status: 'loaded', value }) },
      (error) => { if (!cancelled) setFileState({ status: 'error', error: describeError(error) }) },
    )
    return () => { cancelled = true }
  }, [change.diff, change.path, runId])

  useEffect(() => {
    if (mode !== 'diff') return
    const target = hunkRefs.current[activeHunk]
    target?.scrollIntoView({ block: 'center', behavior: 'auto' })
  }, [activeHunk, mode, rows.length])

  function moveHunk(delta: number) {
    if (!hunkIndexes.length) return
    setActiveHunk((current) => (current + delta + hunkIndexes.length) % hunkIndexes.length)
  }

  const contentLines = fileState.value?.content ? currentFileLines(fileState.value.content) : []
  const binary = change.binary || fileState.value?.binary
  return <aside className="file-change-panel is-open" aria-label="文件变更详情">
    <header className="file-change-panel-header">
      <div className="file-change-panel-title"><FileCode2 size={15} aria-hidden="true" /><div><strong>文件变更</strong><span title={change.path}>{change.path}</span></div></div>
      <button type="button" className="icon-button" onClick={onClose} aria-label="关闭文件变更详情"><X size={16} /></button>
    </header>
    <div className="file-change-panel-summary">
      <span className={`file-change-status is-${change.operation}`}>{operationLabel(change.operation)}</span>
      <span className="file-change-panel-counts"><b>+{change.added_lines || 0}</b><em>−{change.deleted_lines || 0}</em></span>
      {hunkIndexes.length > 1 && <div className="file-change-hunk-nav" aria-label="变更位置导航">
        <button type="button" onClick={() => moveHunk(-1)} aria-label="上一个变更位置"><ChevronLeft size={14} /></button>
        <span>{activeHunk + 1}/{hunkIndexes.length}</span>
        <button type="button" onClick={() => moveHunk(1)} aria-label="下一个变更位置"><ChevronRight size={14} /></button>
      </div>}
    </div>
    <div className="file-change-panel-tabs" role="tablist" aria-label="文件视图">
      <button type="button" role="tab" aria-selected={mode === 'diff'} className={mode === 'diff' ? 'active' : ''} onClick={() => setMode('diff')}>变更</button>
      <button type="button" role="tab" aria-selected={mode === 'file'} className={mode === 'file' ? 'active' : ''} onClick={() => setMode('file')}>当前文件</button>
    </div>
    {mode === 'diff' ? <div className="file-change-diff" role="region" aria-label="代码差异">
      {binary ? <div className="file-change-empty">这是二进制文件，无法显示文本差异。</div> : rows.length ? rows.map((row, index) => row.kind === 'hunk'
        ? <div key={`hunk-${index}`} ref={(node) => { hunkRefs.current[row.hunk ?? 0] = node }} className={`file-change-diff-row is-hunk ${(row.hunk ?? 0) === activeHunk ? 'is-focused' : ''}`}><span className="file-change-diff-marker" /><code>{row.text}</code></div>
        : <div key={`row-${index}`} className={`file-change-diff-row is-${row.kind}`}><span className="file-change-old-line">{lineNumber(row.oldLine)}</span><span className="file-change-new-line">{lineNumber(row.newLine)}</span><span className="file-change-diff-marker">{row.kind === 'add' ? '+' : row.kind === 'delete' ? '−' : ' '}</span><code>{row.text || ' '}</code></div>,
      ) : <div className="file-change-empty">此变更没有可读取的文本 diff。</div>}
      {change.diff_truncated && <p className="file-change-truncated">变更内容较大，当前仅显示前一部分。</p>}
    </div> : <div className="file-change-file-view" role="region" aria-label="当前文件内容">
      {fileState.status === 'loading' && <div className="file-change-empty">正在读取当前文件…</div>}
      {fileState.status === 'error' && <div className="file-change-empty">{change.operation === 'delete' ? '文件已删除，当前工作区没有可读取内容。' : fileState.error || '无法读取当前文件。'}</div>}
      {fileState.status === 'loaded' && !binary && <>{contentLines.map((line, index) => <div className="file-change-file-row" key={`line-${index}`}><span>{index + 1}</span><code>{line || ' '}</code></div>)}{fileState.value?.truncated && <p className="file-change-truncated">文件过大，已截断显示。</p>}</>}
      {fileState.status === 'loaded' && binary && <div className="file-change-empty">这是二进制文件，无法显示文本内容。</div>}
    </div>}
  </aside>
}
