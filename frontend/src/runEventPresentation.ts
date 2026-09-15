// 本文件负责 runEventPresentation 相关的前端数据转换、状态判断或应用入口逻辑，供页面层调用。
import type { ApiRecord, Run, RunEvent } from './types'

// RunEventFact、Tone 和 Presentation 组成运行事件卡片的纯展示模型。
export type RunEventFact = { label: string; value: string }
export type RunEventTone = 'neutral' | 'active' | 'success' | 'warning' | 'danger'

export type RunEventPresentation = {
  title: string
  detail: string
  facts: RunEventFact[]
  tone: RunEventTone
}

// 工具完成事件是内部收尾信号，正文时间线只保留工具开始调用，避免同一调用重复占位。
export function isHiddenRunEvent(event: RunEvent): boolean {
  const type = text(event.type) || text(event.event_type) || ''
  return type === 'tool_finished' || type === 'tool_result'
}

// 事件标题只覆盖公开契约中的已知事件；未知类型统一使用中性描述。
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

const diagnosticFactLabels: Record<string, string> = {
  attempt: '重试次数',
  retry_attempt: '重试次数',
  retry_count: '重试次数',
  delay_seconds: '重试等待',
  retry_delay_ms: '重试等待',
  retryable: '可重试',
  duration_ms: '耗时',
  elapsed_ms: '耗时',
  thought_duration_ms: '模型思考耗时',
  code: '错误代码',
  error_code: '错误代码',
  error_kind: '错误类型',
  error_type: '错误类型',
  child_run_id: '子运行 ID',
  child_agent_id: '子 Agent ID',
  delegation_id: '委派 ID',
  task_id: '子任务 ID',
  background_job_id: '后台任务 ID',
  job_id: '后台任务 ID',
}

const diagnosticFactKeys = Object.keys(diagnosticFactLabels)

// 以下窄化和格式化函数把不可信事件 payload 转成稳定、长度受控的展示文本。
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
  return `${Math.round(milliseconds / 1_000)} 秒`
}

// 安全诊断字段只接受标量；对象和数组不会被序列化到界面。
function formatFactValue(key: string, value: unknown): string {
  const numeric = number(value)
  if (key.endsWith('_ms') && numeric !== undefined) return formatDuration(numeric)
  if (key === 'delay_seconds' && numeric !== undefined) return `${numeric} 秒`
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (numeric !== undefined) return new Intl.NumberFormat('zh-CN').format(numeric)
  if (typeof value === 'string') return truncate(value, 180)
  return ''
}

// 由事件类型和结果状态决定视觉语气。
function toneFor(type: string, payload: ApiRecord): RunEventTone {
  if (type.includes('failed') || type === 'run_interrupted' || type === 'completion_verification_rejected') return 'danger'
  if (type === 'mcp_degraded' || type.includes('stopped') || type === 'approval_requested') return 'warning'
  if (type.includes('completed') || type.endsWith('passed') || type === 'mcp_ready' || type === 'terminal_response_persisted') return 'success'
  if (type.includes('started') || type === 'mcp_connecting' || payload.pending_approval === true) return 'active'
  return 'neutral'
}

// 提炼事件最重要的一行详情，供运行时间线快速扫描。
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

// 把后端 RunEvent 完整转换为事件标题、详情、事实列表和语气。
export function presentRunEvent(event: RunEvent): RunEventPresentation {
  const type = text(event.type) || text(event.event_type) || 'runtime_event'
  const payload = record(event.payload) ?? {}
  const knownEvent = type in eventTitles || ['tool_started', 'tool_call', 'tool_finished', 'tool_result'].includes(type)
  if (!knownEvent) {
    return { title: '运行事件', detail: '记录了一项运行状态变化', facts: [], tone: 'neutral' }
  }
  const toolName = text(payload.tool_name) || '未知工具'
  const toolFailed = payload.ok === false || Boolean(text(payload.error_code))
  const title = type === 'tool_started' || type === 'tool_call'
    ? `开始调用 ${toolName}`
    : type === 'tool_finished' || type === 'tool_result'
      ? toolFailed ? `${toolName} 调用失败` : `${toolName} 调用完成`
      : eventTitles[type]
  const detail = type === 'tool_started' || type === 'tool_call'
    ? '工具正在执行'
    : type === 'tool_finished' || type === 'tool_result'
      ? toolFailed ? '工具返回了失败结果' : '工具已经返回结果'
      : eventDetail(type, payload)
  const facts: RunEventFact[] = []
  for (const key of diagnosticFactKeys) {
    const value = payload[key]
    if (value === undefined || value === null) continue
    const rendered = formatFactValue(key, value)
    if (rendered) facts.push({ label: diagnosticFactLabels[key], value: rendered })
  }
  return { title, detail, facts, tone: toneFor(type, payload) }
}

// 为运行记录生成优先使用目标、计划或 ID 的主标题。
export function runDisplayTitle(run: Run): string {
  return run.session_title || run.title || run.agent_name || `运行 ${run.id.slice(0, 8)}`
}

// 生成运行列表次要说明，补充 Agent 或会话关联。
export function runSecondaryLabel(run: Run): string {
  const kindLabels: Record<string, string> = {
    initial: '初始运行',
    continuation: '继续运行',
    recovery: '恢复运行',
  }
  return [kindLabels[run.run_kind || ''] || '运行', run.agent_name, run.id.slice(0, 8)].filter(Boolean).join(' · ')
}

// 将事件绝对时间转换为本地时分秒。
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

// 计算事件相对运行开始时间，便于分析各阶段耗时。
export function formatRunEventOffset(value: string | undefined, startedAt: string | undefined): string {
  if (!value || !startedAt) return ''
  const elapsed = new Date(value).getTime() - new Date(startedAt).getTime()
  if (!Number.isFinite(elapsed) || elapsed < 0) return ''
  return `+${formatDuration(elapsed)}`
}
