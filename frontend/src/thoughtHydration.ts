export type ThoughtHydrationToken = symbol

/** Tracks history requests without letting a cancelled request look loaded. */
export class ThoughtHydrationRegistry {
  private readonly loaded = new Set<string>()
  private readonly pending = new Map<string, ThoughtHydrationToken>()

  reset() {
    this.loaded.clear()
    this.pending.clear()
  }

  shouldLoad(runId: string) {
    return !this.loaded.has(runId) && !this.pending.has(runId)
  }

  isLoaded(runId: string) {
    return this.loaded.has(runId)
  }

  isPending(runId: string) {
    return this.pending.has(runId)
  }

  begin(runId: string): ThoughtHydrationToken | null {
    if (!this.shouldLoad(runId)) return null
    const token = Symbol(runId)
    this.pending.set(runId, token)
    return token
  }

  complete(runId: string, token: ThoughtHydrationToken) {
    if (this.pending.get(runId) !== token) return false
    this.pending.delete(runId)
    this.loaded.add(runId)
    return true
  }

  cancel(runId: string, token: ThoughtHydrationToken) {
    if (this.pending.get(runId) === token) this.pending.delete(runId)
  }
}
