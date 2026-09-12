// 审批内容需要足够帮助用户判断，同时对常见凭据字段做界面层脱敏。
const sensitiveApprovalKey = /(api[_-]?key|token|password|secret|credential|authorization)/i

export function formatApprovalArguments(toolName: string | undefined, argumentsValue: Record<string, unknown> | undefined): string {
  if (!argumentsValue || !Object.keys(argumentsValue).length) return '无参数'
  const safeArguments = Object.fromEntries(Object.entries(argumentsValue).map(([key, value]) => [
    key,
    sensitiveApprovalKey.test(key) ? '[已隐藏]' : value,
  ]))
  if (toolName?.toLowerCase() === 'shell' && (typeof safeArguments.command === 'string' || Array.isArray(safeArguments.command))) {
    const command = Array.isArray(safeArguments.command) ? safeArguments.command.join(' ') : String(safeArguments.command)
    const context = Object.fromEntries(Object.entries(safeArguments).filter(([key]) => key !== 'command'))
    return Object.keys(context).length
      ? `命令：${command}\n${JSON.stringify(context, null, 2)}`
      : command
  }
  try {
    return JSON.stringify(safeArguments, null, 2)
  } catch {
    return '参数无法展示'
  }
}
