export const MAX_ATTACHMENT_COUNT = 10
export const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
export const MAX_ATTACHMENT_TOTAL_BYTES = 75 * 1024 * 1024

export interface PendingAttachment {
  id: string
  file: File
  previewUrl: string
}

export interface AttachmentSummary {
  id: string
  name: string
  mime_type: string
  size_bytes: number
  kind: string
}

function fileIdentity(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}:${file.type}`
}

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

export function attachmentSignature(files: File[]): string {
  return files.map(fileIdentity).join('|')
}

export function attachmentForm(payload: unknown, files: File[]): FormData {
  const form = new FormData()
  form.append('payload', JSON.stringify(payload))
  files.forEach((file) => form.append('files', file, file.name))
  return form
}

export function formatAttachmentSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`
}

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
