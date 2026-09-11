export type ComposerValueState = { resetKey: string; value: string }

// resetKey 标识输入所属会话；变化时必须丢弃旧会话草稿。
export function alignComposerValueState(state: ComposerValueState, resetKey: string): ComposerValueState {
  return state.resetKey === resetKey ? state : { resetKey, value: '' }
}
