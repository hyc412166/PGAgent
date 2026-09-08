// 本测试文件验证 attachments 模块的公开行为与关键边界，确保相关组件或纯函数在重构后保持既定契约。
import { describe, expect, it } from 'vitest'
import { attachmentForm, attachmentSignature, formatAttachmentSize, messageAttachments, selectAttachmentFiles } from './attachments'

// 测试分组：会话附件。
describe('会话附件', () => {
  // 测试场景：构造 multipart 消息并保留重复 files 字段。
  it('构造 multipart 消息并保留重复 files 字段', () => {
    const first = new File(['one'], 'one.txt', { type: 'text/plain', lastModified: 1 })
    const second = new File(['two'], 'two.txt', { type: 'text/plain', lastModified: 2 })
    const form = attachmentForm({ content: '分析', idempotency_key: 'turn-1' }, [first, second])

    expect(JSON.parse(String(form.get('payload')))).toEqual({ content: '分析', idempotency_key: 'turn-1' })
    expect(form.getAll('files')).toEqual([first, second])
    expect(attachmentSignature([first, second])).not.toBe(attachmentSignature([second, first]))
  })

  // 测试场景：拒绝单个超限文件并忽略已经选择的同一文件。
  it('拒绝单个超限文件并忽略已经选择的同一文件', () => {
    const existing = new File(['same'], 'same.txt', { type: 'text/plain', lastModified: 1 })
    const duplicate = new File(['same'], 'same.txt', { type: 'text/plain', lastModified: 1 })
    const oversized = new File([new Uint8Array(25 * 1024 * 1024 + 1)], 'large.bin')

    expect(selectAttachmentFiles([duplicate], [existing])).toEqual({ files: [], error: '' })
    expect(selectAttachmentFiles([oversized], [])).toEqual({ files: [], error: 'large.bin 超过 25 MB' })
  })

  // 测试场景：格式化大小并只接收结构完整的消息附件。
  it('格式化大小并只接收结构完整的消息附件', () => {
    expect(formatAttachmentSize(1536)).toBe('1.5 KB')
    expect(messageAttachments([
      { id: 'file-1', name: 'report.pdf', mime_type: 'application/pdf', size_bytes: 2048, kind: 'user_attachment' },
      { name: 'missing-id' },
    ])).toEqual([{ id: 'file-1', name: 'report.pdf', mime_type: 'application/pdf', size_bytes: 2048, kind: 'user_attachment' }])
  })
})
