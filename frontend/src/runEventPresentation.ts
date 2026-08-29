import type { ApiRecord, Run, RunEvent } from './types'

export type RunEventFact = { label: string; value: string }
export type RunEventTone = 'neutral' | 'active' | 'success' | 'warning' | 'danger'

export type RunEventPresentation = {
  title: string
  detail: string
  facts: RunEventFact[]
  tone: RunEventTone
}

const eventTitles: Record<string, string> = {
  approval_rejected: '审批已拒绝',
  approval_requested: '等待工具审批',
  completed: '运行完成',
  context_compacted: '上下文压缩完成',
  context_compaction_failed: '上下文压缩失败',
  context_compaction_finished: '上下文整理完成',
  context_compaction_started: '开始整理上下文',
  context_prepared: '上下文已准备',
  context_protocol_repaired: '上下文协议已修复',
  context_resumed: '已恢复运行上下文',
  delegated_child_awaiting_approval: '子 Agent 等待审批',
  delegated_child_completed: '子 Agent 已完成',
  delegated_child_continuation_started: '开始汇总子 Agent 结果',
  delegated_child_failed: '子 Agent 执行失败',
  delegated_child_started: '子 Agent 已启动',
  delegated_child_stopped: '子 Agent 已停止',
  failed: '运行失败',
  integration_failed: '运行集成失败',
  mcp_catalog_loading: '正在准备 MCP 工具目录',
  mcp_connecting: '正在连接 MCP 服务',
  mcp_degraded: '部分 MCP 服务不可用',
  mcp_ready: 'MCP 服务已就绪',
  mcp_server_ready: 'MCP 服务已按需连接',
  model_failed: '模型调用失败',
  model_retry: '正在重试模型',
  model_step_started: '开始模型步骤',
  completion_verification_passed: '结果验收通过',
  completion_verification_rejected: '结果验收未通过',
  completion_verification_started: '开始验收结果',
  progress: '运行进度',
  agent_progress: 'Agent 进度',
  activity_update: '运行活动',
  thought_summary: '模型进度摘要',
  run_completed: '运行完成',
  run_interrupted: '运行已中断',
  run_stopped: '运行已停止',
  stopped: '运行已停止',
  terminal_response_persisted: '最终回复已保存',
  user_question_requested: '等待用户补充信息',
}

const factLabels: Record<string, string> = {
  accepted: '验收结果',
  affected_call_count: '受影响调用',
  after_tokens: '整理后上下文',
  attempt: '尝试次数',
  before_tokens: '整理前上下文',
  changed: '产生变更',
  code: '状态代码',
  complete: '步骤完成',
  delay_seconds: '重试等待',
  duration_ms: '本阶段耗时',
  elapsed_ms: '运行到',
  error_code: '错误代码',
  error_kind: '错误类型',
  error_type: '错误类型',
  estimated_tokens: '上下文',
  failed_servers: '不可用服务',
  has_output: '生成回复',
  message_id: '消息 ID',
  model: '模型',
  ok: '调用成功',
  omitted_messages: '省略历史消息',
  output_chars: '回复长度',
  pending_approval: '等待审批',
  phase: '阶段',
  question_chars: '问题长度',
  reason: '原因',
  remaining_call_count: '剩余调用',
  removed_messages: '移除历史消息',
  requires_next_message: '需要下一条消息',
  servers: 'MCP 服务',
  source: '结果来源',
  status: '状态',
  step: '步骤',
  thought_duration_ms: '模型思考耗时',
  tool_call_id: '调用 ID',
  tool_count: '可用工具',
  trace_id: '追踪 ID',
  turn_id: '对话轮次 ID',
}

const argumentLabels: Record<string, string> = {
  command: '命令',
  depth: '快照深度',
  element: '元素描述',
  file_path: '文件',
  path: '路径',
  pattern: '匹配规则',
  query: '查询内容',
  recursive: '递归',
  submit: '提交',
  target: '目标',
  task: '子任务',
  text: '输入内容',
  todos: '任务项',
  url: '网址',
}

function record(value: unknown): ApiRecord | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as ApiRecord : undefined
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function number(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function truncate(value: string, limit = 240): string {
  const normalized = value.replace(/\s+/g, ' ').trim()
  return normalized.length <= limit ? normalized : `${normalized.slice(0, limit - 1).trimEnd()}…`
}

function formatDuration(milliseconds: number): string {
  if (milliseconds < 1_000) return `${Math.max(0, Math.round(milliseconds))} 毫秒`
  const seconds = milliseconds / 1_000
  return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} 秒`
}

function formatArgumentValue(key: string, value: unknown): string {
  const structured = record(value)
  if (key === 'command' && structured) {
    const executable = text(structured.executable) || '已提供命令'
    const count = number(structured.argument_count)
    return count === undefined ? executable : `${executable} · ${count} 个参数`
  }
  if (structured) {
    const chars = number(structured.chars)
    if (chars !== undefined) return `${chars} 个字符`
    const count = number(structured.count)
    if (count !== undefined) return `${count} 项`
    const argumentCount = number(structured.argument_count)
    if (argumentCount !== undefined) return `${argumentCount} 个参数`
    if (typeof structured.provided === 'boolean') return structured.provided ? '已提供' : '未提供'
  }
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return new Intl.NumberFormat('zh-CN').format(value)
  if (typeof value === 'string') return truncate(value, 180)
  return truncate(JSON.stringify(value), 180)
}

function formatFactValue(key: string, value: unknown): string {
  const numeric = number(value)
  if (key.endsWith('_ms') && numeric !== undefined) return formatDuration(numeric)
  if (key === 'delay_seconds' && numeric !== undefined) return `${numeric} 秒`
  if (['estimated_tokens', 'before_tokens', 'after_tokens'].includes(key) && numeric !== undefined) {
    return `${new Intl.NumberFormat('zh-CN').format(numeric)} Token`
  }
  if (['omitted_messages', 'removed_messages'].includes(key) && numeric !== undefined) return `${numeric} 条`
  if (key === 'tool_count' && numeric !== undefined) return `${numeric} 个`
  if (key === 'output_chars' && numeric !== undefined) return `${numeric} 个字符`
  if (key === 'question_chars' && numeric !== undefined) return `${numeric} 个字符`
  if (key === 'remaining_call_count' && numeric !== undefined) return `${numeric} 次`
  if (key === 'affected_call_count' && numeric !== undefined) return `${numeric} 次`
  if (key === 'step' && numeric !== undefined) return `第 ${numeric} 步`
  if (typeof value === 'string') {
    const controlledValues: Record<string, string> = {
      cancelled: '已取消',
      completed: '已完成',
      failed: '失败',
      model: '模型',
      model_output: '模型输出',
      running: '运行中',
      stopped: '已停止',
    }
    if (controlledValues[value]) return controlledValues[value]
  }
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (numeric !== undefined) return new Intl.NumberFormat('zh-CN').format(numeric)
  if (Array.isArray(value)) {
    const rendered = value.map((item) => {
      if (typeof item === 'string') return item
      const itemRecord = record(item)
      return itemRecord ? [text(itemRecord.name), text(itemRecord.error_code)].filter(Boolean).join('：') : ''
    }).filter(Boolean)
    return rendered.length ? rendered.join('、') : '无'
  }
  if (typeof value === 'string') return truncate(value, 180)
  return truncate(JSON.stringify(value), 180)
}

function toneFor(type: string, payload: ApiRecord): RunEventTone {
  if (type.includes('failed') || type === 'run_interrupted' || type === 'completion_verification_rejected') return 'danger'
  if (type === 'mcp_degraded' || type.includes('stopped') || type === 'approval_requested') return 'warning'
  if (type.includes('completed') || type.endsWith('passed') || type === 'mcp_ready' || type === 'terminal_response_persisted') return 'success'
  if (type.includes('started') || type === 'mcp_connecting' || payload.pending_approval === true) return 'active'
  return 'neutral'
}

function eventDetail(type: string, payload: ApiRecord): string {
  const message = text(payload.message_excerpt)
  if (type === 'context_prepared' || type === 'context_resumed') {
    return message ? `用户消息：${message}` : '已整理本轮模型需要的消息和系统上下文'
  }
  if (type === 'mcp_catalog_loading') {
    const servers = formatFactValue('servers', payload.servers)
    return servers === '无' ? '正在读取缓存或发现 MCP 工具' : `正在准备：${servers}`
  }
  if (type === 'mcp_connecting') {
    const servers = formatFactValue('servers', payload.servers)
    return servers === '无' ? '正在启动并发现 MCP 工具' : `正在连接：${servers}`
  }
  if (type === 'mcp_ready') return 'MCP 工具目录已经可以使用'
  if (type === 'mcp_server_ready') return `${text(payload.server) || 'MCP 服务'} 已完成按需连接`
  if (type === 'mcp_degraded') return '本轮将继续使用已经连接的 MCP 工具'
  if (type === 'thought_summary') return truncate(text(payload.summary) || '模型已更新处理思路')
  if (['progress', 'agent_progress', 'activity_update'].includes(type)) {
    return truncate(text(payload.progress) || text(payload.status_text) || text(payload.activity) || '运行状态已更新')
  }
  if (type === 'model_step_started') {
    const step = number(payload.step)
    return step === undefined ? '模型开始处理下一步' : `模型开始处理第 ${step} 步`
  }
  if (type === 'model_retry') return text(payload.reason) || '上一次模型请求未成功，正在重试'
  if (type.startsWith('completion_verification_')) return text(payload.failure_reason) || '正在检查任务是否满足完成条件'
  if (type.startsWith('delegated_child_')) {
    return text(payload.task_title) || text(payload.child_agent_name) || '子 Agent 状态已更新'
  }
  if (type === 'approval_requested') {
    const request = record(payload.request)
    const tool = text(request?.tool_name)
    return tool ? `工具 ${tool} 需要你的确认` : '下一项操作需要你的确认'
  }
  if (type === 'terminal_response_persisted') return '最终回复已经写入会话记录'
  if (type === 'run_completed' || type === 'completed') return '本次运行已正常结束'
  if (type === 'run_interrupted') return '用户中断了本次运行'
  if (type === 'run_stopped' || type === 'stopped') return '本次运行已停止'
  if (type.includes('failed')) return text(payload.reason) || text(payload.error_type) || '本次运行未能继续'
  if (type.startsWith('context_compaction_') || type === 'context_compacted') return '正在控制上下文长度并保留任务状态'
  return text(payload.summary) || text(payload.reason) || '运行状态已更新'
}

export function presentRunEvent(event: RunEvent): RunEventPresentation {
  const type = text(event.type) || text(event.event_type) || 'runtime_event'
  const payload = record(event.payload) ?? {}
  const toolName = text(payload.tool_name) || '未知工具'
  const title = type === 'tool_started' || type === 'tool_call'
    ? `开始调用 ${toolName}`
    : type === 'tool_finished' || type === 'tool_result'
      ? payload.ok === false ? `${toolName} 调用失败` : `${toolName} 调用完成`
      : eventTitles[type] || type.replaceAll('_', ' ')
  const detail = type === 'tool_started' || type === 'tool_call'
    ? '工具正在执行'
    : type === 'tool_finished' || type === 'tool_result'
      ? payload.ok === false ? '工具返回了失败结果' : '工具已经返回结果'
      : eventDetail(type, payload)
  const facts: RunEventFact[] = []
  const argumentsRecord = record(payload.arguments)
  if (argumentsRecord) {
    for (const [key, value] of Object.entries(argumentsRecord)) {
      facts.push({ label: argumentLabels[key] || key, value: formatArgumentValue(key, value) })
    }
  }
  const request = record(payload.request)
  if (request) {
    const requestedTool = text(request.tool_name)
    if (requestedTool) facts.push({ label: '工具', value: requestedTool })
    const requestArguments = record(request.arguments)
    if (requestArguments) {
      for (const [key, value] of Object.entries(requestArguments)) {
        facts.push({ label: argumentLabels[key] || key, value: formatArgumentValue(key, value) })
      }
    }
  }
  const excluded = new Set(['arguments', 'request', 'message_excerpt', 'summary', 'progress', 'status_text', 'activity', 'failure_reason', 'partial_output', 'partial_thought', 'tool_name'])
  for (const [key, value] of Object.entries(payload)) {
    if (excluded.has(key) || value === undefined || value === null) continue
    facts.push({ label: factLabels[key] || key, value: formatFactValue(key, value) })
  }
  return { title, detail, facts, tone: toneFor(type, payload) }
}

export function runDisplayTitle(run: Run): string {
  return run.session_title || run.title || run.agent_name || `运行 ${run.id.slice(0, 8)}`
}

export function runSecondaryLabel(run: Run): string {
  const kindLabels: Record<string, string> = {
    initial: '初始运行',
    continuation: '继续运行',
    recovery: '恢复运行',
  }
  return [kindLabels[run.run_kind || ''] || '运行', run.agent_name, run.id.slice(0, 8)].filter(Boolean).join(' · ')
}

export function formatRunEventTime(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export function formatRunEventOffset(value: string | undefined, startedAt: string | undefined): string {
  if (!value || !startedAt) return ''
  const elapsed = new Date(value).getTime() - new Date(startedAt).getTime()
  if (!Number.isFinite(elapsed) || elapsed < 0) return ''
  return `+${formatDuration(elapsed)}`
}
