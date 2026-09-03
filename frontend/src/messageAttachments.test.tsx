import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { MessageBubble } from './features/sessions/presentation'

describe('message image presentation', () => {
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
