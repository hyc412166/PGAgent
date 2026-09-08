// 本文件负责 thoughtHydration 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
// 每次历史思考加载使用唯一 token，取消的旧请求无法将同一运行误标为已完成。
export type ThoughtHydrationToken = symbol

/** 跟踪思考历史加载，确保已取消请求不会被当作加载完成。 */
export class ThoughtHydrationRegistry {
  // loaded 保存已完成运行，pending 保存运行与当前请求 token 的对应关系。
  private readonly loaded = new Set<string>()
  private readonly pending = new Map<string, ThoughtHydrationToken>()

  // 会话切换时清空全部加载状态。
  reset() {
    this.loaded.clear()
    this.pending.clear()
  }

  // 仅未加载且无在途请求的运行需要发起请求。
  shouldLoad(runId: string) {
    return !this.loaded.has(runId) && !this.pending.has(runId)
  }

  isLoaded(runId: string) {
    return this.loaded.has(runId)
  }

  isPending(runId: string) {
    return this.pending.has(runId)
  }

  // 注册请求并返回完成/取消时必须携带的唯一 token。
  begin(runId: string): ThoughtHydrationToken | null {
    if (!this.shouldLoad(runId)) return null
    const token = Symbol(runId)
    this.pending.set(runId, token)
    return token
  }

  // 只有当前 token 能将运行从 pending 原子迁移到 loaded。
  complete(runId: string, token: ThoughtHydrationToken) {
    if (this.pending.get(runId) !== token) return false
    this.pending.delete(runId)
    this.loaded.add(runId)
    return true
  }

  // 取消仍属于该 token 的请求，不影响后来为同一运行发起的新请求。
  cancel(runId: string, token: ThoughtHydrationToken) {
    if (this.pending.get(runId) === token) this.pending.delete(runId)
  }
}
