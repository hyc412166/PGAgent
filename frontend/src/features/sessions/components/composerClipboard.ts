import type { ClipboardEvent } from 'react'

// 图片交给会话附件队列；混合剪贴板里的文字仍由浏览器按光标位置粘贴。
export function pasteComposerImages(event: ClipboardEvent<HTMLTextAreaElement>, onPasteImages?: (files: File[]) => void) {
  const images = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith('image/'))
  if (!images.length) return
  if (!event.clipboardData.getData('text/plain')) event.preventDefault()
  onPasteImages?.(images)
}
