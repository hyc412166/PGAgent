// 本文件负责 attachments 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
// 三个常量分别限制单条消息的附件数量、单文件大小和总大小，与后端上传约束保持一致。
export const MAX_ATTACHMENT_COUNT = 10
export const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
export const MAX_ATTACHMENT_TOTAL_BYTES = 75 * 1024 * 1024

// PendingAttachment 是尚未上传的浏览器 File 及其本地预览信息。
export interface PendingAttachment {
  id: string
  file: File
  previewUrl: string
}

// AttachmentSummary 是消息 metadata 中可持久化和展示的附件摘要。
export interface AttachmentSummary {
  id: string
  name: string
  mime_type: string
  size_bytes: number
  kind: string
}

// 以名称、大小、修改时间和 MIME 类型组合识别同一批次中的重复文件。
function fileIdentity(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}:${file.type}`
}

// 去重并一次性校验新附件；任一边界失败时不接受本批文件。
export function selectAttachmentFiles(selected: File[], existing: File[]): { files: File[]; error: string } {
  const identities = new Set(existing.map(fileIdentity))
  const unique = selected.filter((file) => {
    const identity = fileIdentity(file)
    if (identities.has(identity)) return false
    identities.add(identity)
    return true
  })
  const oversized = unique.find((file) => file.size > MAX_ATTACHMENT_BYTES)
  if (oversized) return { files: [], error: `${oversized.name} 超过 25 MB` }
  if (existing.length + unique.length > MAX_ATTACHMENT_COUNT) {
    return { files: [], error: `每条消息最多附加 ${MAX_ATTACHMENT_COUNT} 个文件` }
  }
  const totalBytes = [...existing, ...unique].reduce((total, file) => total + file.size, 0)
  if (totalBytes > MAX_ATTACHMENT_TOTAL_BYTES) {
    return { files: [], error: '单条消息的附件总大小不能超过 75 MB' }
  }
  return { files: unique, error: '' }
}

// 生成提交重试比较用签名，避免同一文本配不同附件时误用幂等键。
export function attachmentSignature(files: File[]): string {
  return files.map(fileIdentity).join('|')
}

// 将 JSON 运行参数和多个文件封装为后端上传接口所需 multipart 表单。
export function attachmentForm(payload: unknown, files: File[]): FormData {
  const form = new FormData()
  form.append('payload', JSON.stringify(payload))
  files.forEach((file) => form.append('files', file, file.name))
  return form
}

// 按大小选择 B、KB 或 MB，并保留适合界面的精度。
export function formatAttachmentSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`
}

// 从不可信消息 metadata 中筛选结构完整的附件，缺省字段使用安全展示值。
export function messageAttachments(value: unknown): AttachmentSummary[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const record = item as Record<string, unknown>
    if (typeof record.id !== 'string' || typeof record.name !== 'string') return []
    return [{
      id: record.id,
      name: record.name,
      mime_type: typeof record.mime_type === 'string' ? record.mime_type : 'application/octet-stream',
      size_bytes: typeof record.size_bytes === 'number' ? record.size_bytes : 0,
      kind: typeof record.kind === 'string' ? record.kind : 'user_attachment',
    }]
  })
}
