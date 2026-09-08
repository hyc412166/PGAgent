// 本文件实现 projectDeletion 功能域的页面或组件，并把接口数据、交互状态与公共展示组件连接起来。
// 根据项目名称和包含会话数生成删除确认文案，明确级联影响。
export function projectDeleteConfirmation(workspaceName: string, conversationCount: number): string {
  const conversationText = conversationCount
    ? `该项目下的 ${conversationCount} 个历史对话及其运行记录也会永久删除。`
    : '该项目当前没有历史对话。'
  return `确定从 PGAgent 删除项目“${workspaceName}”吗？${conversationText}本地项目文件不会被删除，此操作无法撤销。`
}
