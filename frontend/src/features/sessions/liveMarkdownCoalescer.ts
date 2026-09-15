// 将高频流式正文增量合并为固定节奏的提交，避免每个 token 都触发完整 Markdown/KaTeX 解析。
const LIVE_MARKDOWN_INTERVAL_MS = 100

export function createLiveMarkdownCoalescer(initialContent: string, commit: (content: string) => void) {
  let committedContent = initialContent
  let latestContent = initialContent
  let lastCommittedAt = Date.now()
  let timer: ReturnType<typeof setTimeout> | undefined

  const cancelPending = () => {
    if (timer === undefined) return
    clearTimeout(timer)
    timer = undefined
  }

  return {
    push(content: string) {
      latestContent = content
      if (content === committedContent || timer !== undefined) return
      const delay = Math.max(0, LIVE_MARKDOWN_INTERVAL_MS - (Date.now() - lastCommittedAt))
      timer = setTimeout(() => {
        timer = undefined
        lastCommittedAt = Date.now()
        committedContent = latestContent
        commit(latestContent)
      }, delay)
    },
    flush(content: string) {
      latestContent = content
      cancelPending()
      if (content === committedContent) return
      lastCommittedAt = Date.now()
      committedContent = content
      commit(content)
    },
    dispose: cancelPending,
  }
}
