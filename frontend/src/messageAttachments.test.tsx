// 本测试文件验证 messageAttachments 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { MessageBubble } from './features/sessions/presentation'

// 测试分组：message image presentation。
describe('message image presentation', () => {
  // 测试场景：places a clean image preview above the user text bubble。
  it('places a clean image preview above the user text bubble', () => {
    const markup = renderToStaticMarkup(<MessageBubble message={{
      id: 'message-1',
      session_id: 'session-1',
      role: 'user',
      content: '分析这张图片',
      metadata: {
        attachments: [{
          id: 'image-1',
          name: 'test.png',
          mime_type: 'image/png',
          size_bytes: 67_303,
          kind: 'user_attachment',
        }],
      },
    }} />)

    expect(markup).toContain('class="message-image-gallery"')
    expect(markup.indexOf('message-image-gallery')).toBeLessThan(markup.indexOf('message-content'))
    expect(markup).not.toContain('image/png ·')
  })

  // 测试场景：renders Responses web search citations as user-visible source links。
  it('renders Responses web search citations as user-visible source links', () => {
    const markup = renderToStaticMarkup(<MessageBubble message={{
      id: 'message-2',
      role: 'assistant',
      content: '检索完成。',
      citations: [{ url: 'https://example.com/news', title: 'Example News' }],
    }} />)

    expect(markup).toContain('aria-label="参考来源"')
    expect(markup).toContain('href="https://example.com/news"')
    expect(markup).toContain('Example News')
  })
})
