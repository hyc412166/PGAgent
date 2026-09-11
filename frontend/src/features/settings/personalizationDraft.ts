// null 表示用户尚未编辑，此时跟随后台权威值；字符串则始终优先保留本地草稿。
export function resolvePersonalizationDraft(draft: string | null, persisted: string) {
  return draft ?? persisted
}

export function normalizePersonalizationDraft(draft: string | null, persisted: string) {
  return draft === persisted ? null : draft
}

// 保存完成后只清除本次提交的草稿；请求期间继续输入的新内容必须保留。
export function personalizationDraftAfterSave(currentDraft: string | null, submittedDraft: string) {
  return currentDraft === submittedDraft ? null : currentDraft
}
