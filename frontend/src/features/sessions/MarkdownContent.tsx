// 本组件负责把 Agent 的 Markdown 与 LaTeX 正文转换为富文本，并统一公式、代码块和表格的展示。
import { Children, isValidElement, memo, useEffect, useState, type ComponentProps, type ReactNode } from 'react'
import { CheckCheck, Code2, Copy, XCircle } from 'lucide-react'
import ReactMarkdown, { type Components } from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeKatex from 'rehype-katex'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math-extended'
import 'katex/dist/katex.min.css'

import { createLiveMarkdownCoalescer } from './liveMarkdownCoalescer'

const languageLabels: Record<string, string> = {
  bash: 'Bash',
  css: 'CSS',
  html: 'HTML',
  javascript: 'JavaScript',
  js: 'JavaScript',
  json: 'JSON',
  jsx: 'JSX',
  latex: 'LaTeX',
  markdown: 'Markdown',
  md: 'Markdown',
  plaintext: '纯文本',
  powershell: 'PowerShell',
  ps1: 'PowerShell',
  python: 'Python',
  rust: 'Rust',
  scss: 'SCSS',
  shell: 'Shell',
  sql: 'SQL',
  text: '纯文本',
  ts: 'TypeScript',
  tsx: 'TSX',
  txt: '纯文本',
  typescript: 'TypeScript',
  xml: 'XML',
  yaml: 'YAML',
  yml: 'YAML',
}

function textFromNode(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textFromNode).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return textFromNode(node.props.children)
  return ''
}

function languageFromCode(children: ReactNode) {
  const codeElement = Children.toArray(children).find((child) => isValidElement(child))
  if (!isValidElement<{ className?: string }>(codeElement)) return 'text'
  return codeElement.props.className?.match(/(?:^|\s)language-([^\s]+)/)?.[1]?.toLowerCase() || 'text'
}

function displayLanguage(language: string) {
  const knownLabel = languageLabels[language]
  if (knownLabel) return knownLabel
  return language.length <= 4
    ? language.toUpperCase()
    : `${language.charAt(0).toUpperCase()}${language.slice(1)}`
}

type MarkdownTreeNode = {
  type?: string
  properties?: { className?: unknown }
  children?: MarkdownTreeNode[]
}

// rehype-katex 也会处理 ```math；先改成 latex 代码语言，确保 fenced code 保持源码语义。
function preserveLatexCodeFences() {
  return (tree: MarkdownTreeNode) => {
    const pending = [tree]
    while (pending.length) {
      const node = pending.pop()
      if (!node) continue
      if (node.type === 'element' && node.properties) {
        const rawClassName = node.properties.className
        const classNames = Array.isArray(rawClassName)
          ? rawClassName.map(String)
          : typeof rawClassName === 'string'
            ? rawClassName.split(/\s+/)
            : []
        if (classNames.includes('language-math') && !classNames.includes('math-inline') && !classNames.includes('math-display')) {
          node.properties.className = classNames.map((className) => className === 'language-math' ? 'language-latex' : className)
        }
      }
      if (node.children) pending.push(...node.children)
    }
  }
}

// MarkdownCodeBlock 保留高亮器生成的 token 节点，同时从同一棵节点树提取原始代码用于复制。
function MarkdownCodeBlock({ children }: { children?: ReactNode }) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const language = languageFromCode(children)
  const label = displayLanguage(language)
  const code = textFromNode(children).replace(/\n$/, '')

  async function copyCode() {
    try {
      await navigator.clipboard.writeText(code)
      setCopyState('copied')
      window.setTimeout(() => setCopyState('idle'), 1_400)
    } catch (error) {
      // 复制失败仍保留在 UI 状态中，避免权限或非安全上下文问题被静默吞掉。
      console.error('复制代码失败', error)
      setCopyState('failed')
      window.setTimeout(() => setCopyState('idle'), 2_000)
    }
  }

  return <div className="markdown-code-block">
    <div className="markdown-code-header">
      <span><Code2 size={13} aria-hidden="true" />{label}</span>
      <button type="button" aria-label={copyState === 'copied' ? `已复制 ${label} 代码` : copyState === 'failed' ? `复制 ${label} 代码失败` : `复制 ${label} 代码`} title={copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败，请重试' : '复制代码'} onClick={() => void copyCode()}>
        {copyState === 'copied' ? <CheckCheck size={14} /> : copyState === 'failed' ? <XCircle size={14} /> : <Copy size={14} />}
      </button>
    </div>
    <pre>{children}</pre>
  </div>
}

const markdownComponents: Components = {
  a({ children, href, title }) {
    return <a href={href} title={title} target="_blank" rel="noreferrer">{children}</a>
  },
  pre({ children }) {
    return <MarkdownCodeBlock>{children}</MarkdownCodeBlock>
  },
  table({ children }) {
    return <div className="markdown-table-wrap"><table>{children}</table></div>
  },
}

const remarkPlugins: NonNullable<ComponentProps<typeof ReactMarkdown>['remarkPlugins']> = [[remarkMath, { backslashDelimiters: true, singleDollarTextMath: false }], remarkGfm, remarkBreaks]
const rehypePlugins: NonNullable<ComponentProps<typeof ReactMarkdown>['rehypePlugins']> = [preserveLatexCodeFences, [rehypeHighlight, { detect: false, plainText: ['text', 'plaintext', 'txt', 'math'] }], rehypeKatex]

const MarkdownDocument = memo(function MarkdownDocument({ content }: { content: string }) {
  return <ReactMarkdown
    components={markdownComponents}
    remarkPlugins={remarkPlugins}
    rehypePlugins={rehypePlugins}
  >
    {content}
  </ReactMarkdown>
})

function MarkdownContentView({ content, streaming = false }: { content: string; streaming?: boolean }) {
  const [renderedContent, setRenderedContent] = useState(content)
  const [coalescer] = useState(() => createLiveMarkdownCoalescer(content, setRenderedContent))

  useEffect(() => {
    if (streaming) coalescer.push(content)
    else coalescer.flush(content)
  }, [coalescer, content, streaming])

  useEffect(() => () => coalescer.dispose(), [coalescer])

  // 流式正文最多每 100ms 触发一次完整 Markdown/KaTeX 解析；结束态直接使用最终内容。
  const visibleContent = streaming ? renderedContent : content
  return <div className="message-content markdown-content">
    <MarkdownDocument content={visibleContent} />
  </div>
}

// 历史消息正文不可变时跳过重复 Markdown 解析，避免流式增量导致整段会话重绘。
export const MarkdownContent = memo(MarkdownContentView)
