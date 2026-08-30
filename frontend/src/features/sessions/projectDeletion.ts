export function projectDeleteConfirmation(workspaceName: string, conversationCount: number): string {
  const conversationText = conversationCount
    ? `该项目下的 ${conversationCount} 个历史对话及其运行记录也会永久删除。`
    : '该项目当前没有历史对话。'
  return `确定从 PGAgent 删除项目“${workspaceName}”吗？${conversationText}本地项目文件不会被删除，此操作无法撤销。`
}
