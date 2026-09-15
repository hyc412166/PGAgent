// 本组件负责把 Agent 的 Markdown 正文转换为可读的富文本，并统一代码块与表格的交互样式。
import { Children, isValidElement, memo, useState, type ReactNode } from 'react'
import { CheckCheck, Code2, Copy, XCircle } from 'lucide-react'
import ReactMarkdown, { type Components } from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'

const languageLabels: Record<string, string> = {
  bash: 'Bash',
  css: 'CSS',
  html: 'HTML',
  javascript: 'JavaScript',
  js: 'JavaScript',
  json: 'JSON',
  jsx: 'JSX',
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

const remarkPlugins = [remarkGfm, remarkBreaks]
const rehypePlugins: Array<[typeof rehypeHighlight, { detect: boolean; plainText: string[] }]> = [[rehypeHighlight, { detect: false, plainText: ['text', 'plaintext', 'txt'] }]]

function MarkdownContentView({ content }: { content: string }) {
  return <div className="message-content markdown-content">
    <ReactMarkdown
      components={markdownComponents}
      remarkPlugins={remarkPlugins}
      rehypePlugins={rehypePlugins}
    >
      {content}
    </ReactMarkdown>
  </div>
}

// 历史消息正文不可变时跳过重复 Markdown 解析，避免流式增量导致整段会话重绘。
export const MarkdownContent = memo(MarkdownContentView)
