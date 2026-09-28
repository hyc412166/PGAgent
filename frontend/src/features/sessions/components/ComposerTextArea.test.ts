// 本测试验证编辑器在会话身份变化时丢弃旧草稿，避免文本串到另一个会话。
import type { ClipboardEvent } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { pasteComposerImages } from './composerClipboard'
import { attachmentForm, selectAttachmentFiles } from '../../../attachments'
import { alignComposerValueState } from './composerTextAreaState'

describe('ComposerTextArea resetKey', () => {
  it('resetKey 变化后清空旧输入', () => {
    expect(alignComposerValueState({ resetKey: 'session-a', value: '旧会话草稿' }, 'session-b')).toEqual({
      resetKey: 'session-b',
      value: '',
    })
  })
})

function pasteEvent(files: File[], text = '') {
  return {
    clipboardData: { files, getData: (type: string) => type === 'text/plain' ? text : '' },
    preventDefault: vi.fn(),
  } as unknown as ClipboardEvent<HTMLTextAreaElement>
}

describe('输入框粘贴图片', () => {
  const first = new File(['first'], 'first.png', { type: 'image/png', lastModified: 1 })
  const second = new File(['second'], 'second.jpg', { type: 'image/jpeg', lastModified: 2 })

  it.each([[first], [first, second]])('图片进入附件队列并保留上传内容：%j', (...images) => {
    const event = pasteEvent(images)
    const queue = vi.fn()
    pasteComposerImages(event, queue)

    expect(event.preventDefault).toHaveBeenCalledOnce()
    expect(queue).toHaveBeenCalledExactlyOnceWith(images)
    const selected = selectAttachmentFiles(queue.mock.calls[0][0], [])
    const form = attachmentForm({ content: '' }, selected.files)
    expect(selected.error).toBe('')
    expect(form.getAll('files')).toEqual(images)
  })

  it('普通文本和图片网址保留浏览器默认粘贴，不创建附件', () => {
    for (const text of ['普通文字', 'https://example.com/image.png']) {
      const event = pasteEvent([], text)
      const queue = vi.fn()
      pasteComposerImages(event, queue)
      expect(event.preventDefault).not.toHaveBeenCalled()
      expect(queue).not.toHaveBeenCalled()
    }
  })

  it('混合内容仅提取图片，同时保留文本粘贴', () => {
    const event = pasteEvent([first, new File(['document'], 'note.txt', { type: 'text/plain' }), second], '图片说明')
    const queue = vi.fn()
    pasteComposerImages(event, queue)
    expect(queue).toHaveBeenCalledExactlyOnceWith([first, second])
    expect(event.preventDefault).not.toHaveBeenCalled()
  })

  it('附件入口禁用时不入队，仍允许文字粘贴', () => {
    const event = pasteEvent([first], '后续消息')
    expect(() => pasteComposerImages(event)).not.toThrow()
    expect(event.preventDefault).not.toHaveBeenCalled()
  })

  it('粘贴图片沿用附件数量、单文件大小和总大小限制', () => {
    const selected = (files: File[], existing: File[] = []) => {
      let result: ReturnType<typeof selectAttachmentFiles> | undefined
      pasteComposerImages(pasteEvent(files), (images) => { result = selectAttachmentFiles(images, existing) })
      return result
    }
    const existing = Array.from({ length: 10 }, (_, index) => new File(['image'], `${index}.png`, { type: 'image/png' }))
    expect(selected([first], existing)).toEqual({ files: [], error: '每条消息最多附加 10 个文件' })
    expect(selected([new File([new Uint8Array(25 * 1024 * 1024 + 1)], 'large.png', { type: 'image/png' })]))
      .toEqual({ files: [], error: 'large.png 超过 25 MB' })
    const large = Array.from({ length: 4 }, (_, index) => new File([new Uint8Array(20 * 1024 * 1024)], `${index}.png`, { type: 'image/png' }))
    expect(selected(large)).toEqual({ files: [], error: '单条消息的附件总大小不能超过 75 MB' })
  })
})
