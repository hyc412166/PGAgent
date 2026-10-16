// 本测试文件验证 Markdown 链接在会话上下文中的可观察导航结果。
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { MarkdownContent } from './MarkdownContent'
import { LiveAssistantMessage, MessageBubble } from './presentation'
import { emptyThoughtTimeline } from '../../thoughtTimeline'

describe('Markdown 文件链接', () => {
  it('将带运行上下文的本地 Markdown 链接指向原始文件接口', () => {
    const markup = renderToStaticMarkup(createElement(MarkdownContent, {
      content: '[论文完成计划.md](论文完成计划.md)',
      runId: 'run/with-slash',
    }))

    expect(markup).toContain('href="/api/runs/run%2Fwith-slash/file?path=%E8%AE%BA%E6%96%87%E5%AE%8C%E6%88%90%E8%AE%A1%E5%88%92.md"')
    expect(markup).not.toContain('href="论文完成计划.md"')
    expect(markup).toContain('target="_blank"')
  })

  it('保留外部链接的原有新标签行为', () => {
    const markup = renderToStaticMarkup(createElement(MarkdownContent, {
      content: '[文档](https://example.com/guide.md)',
      runId: 'run-1',
    }))

    expect(markup).toContain('href="https://example.com/guide.md"')
    expect(markup).toContain('target="_blank"')
  })

  it.each([
    ['docs/plan%20draft.md', 'docs%2Fplan%20draft.md'],
    ['plan%2Emd', 'plan.md'],
    ['./plan.md#part', 'plan.md'],
    ['docs/plan%23draft.md', 'docs%2Fplan%23draft.md'],
    ['../outside.md', '..%2Foutside.md'],
  ])('将 %s 交给文件接口解析而不是 SPA 路由', (href, encodedPath) => {
    const markup = renderToStaticMarkup(createElement(MarkdownContent, {
      content: `[文件](${href})`, runId: 'run-1',
    }))
    expect(markup).toContain(`href="/api/runs/run-1/file?path=${encodedPath}"`)
  })

  it('历史消息与实时最终回复都传递所属运行', () => {
    const content = '[plan.md](plan.md)'
    const history = renderToStaticMarkup(createElement(MessageBubble, {
      message: { id: 'msg-1', role: 'assistant', content, metadata: { run_id: 'history-run' } },
    }))
    const live = renderToStaticMarkup(createElement(LiveAssistantMessage, {
      liveRun: { runId: 'live-run', phase: '已完成', draft: content, status: 'terminal', error: '', thinkingStatus: '', thought: emptyThoughtTimeline },
    }))
    expect(history).toContain('href="/api/runs/history-run/file?path=plan.md"')
    expect(live).toContain('href="/api/runs/live-run/file?path=plan.md"')
  })

  it('保留 HTML 和危险协议的原有过滤', () => {
    const markup = renderToStaticMarkup(createElement(MarkdownContent, {
      content: '<script>alert(1)</script>\n\n[危险](javascript:alert(1)) ![图片](image.md)', runId: 'run-1',
    }))
    expect(markup).not.toContain('<script>')
    expect(markup).not.toContain('href="javascript:')
    expect(markup).toContain('src="image.md"')
  })
})
