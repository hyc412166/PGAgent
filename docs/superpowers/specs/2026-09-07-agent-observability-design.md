# PGAgent Agent 可观察性与日志设计

## 1. 背景

PGAgent 已经具备持久化 `RunEvent`、进程内 SSE broker 和少量模块级 `logger`，但这些能力尚未组成完整的诊断链路：

- `RunEvent` 能记录部分模型、工具和终态事件，但缺少统一 trace、稳定事件顺序和完整异常上下文；
- SSE、历史事件 API、审批、后台任务与协作事件存在不同的字段投影路径，安全边界不一致；
- `PGAGENT_LOG_LEVEL` 已存在于配置，但应用没有统一的结构化文件日志、轮转和保留策略；
- 模型调用、工具调用、后台任务、子 Agent 和恢复流程发生异常时，无法从一条记录稳定追踪到整个 Run。

本设计新增一个轻量的可观察性模块，复用现有运行事件机制，不引入远程日志服务或新的外部基础设施。

## 2. 目标

1. Agent 每个模型步骤、工具调用、审批等待、后台等待、子 Agent 委派、恢复和终态都有可关联的记录。
2. 本地结构化日志保留排障所需的异常堆栈和运行上下文。
3. `RunEvent`、SSE、API 和 UI 只暴露脱敏后的用户可见信息。
4. 日志写入故障不得让 Agent 重试模型、重复输出或停留在错误的运行状态。
5. 默认控制磁盘占用：单文件最大 20 MB，按天分片，保留 14 天。
6. 在不删除现有安全措施的前提下，统一当前分散的事件投影和脱敏逻辑。

## 3. 非目标

- 不接入 Elasticsearch、OpenTelemetry Collector、Sentry 或其他远程服务。
- 不记录完整模型 prompt、模型响应、工具结果正文、文件内容、stdout/stderr 或完整审批参数。
- 不开放读取本地诊断日志文件的 HTTP API。
- 不自动清理数据库中的 `RunEvent`；其生命周期继续跟随所属 Run/Session。
- 不借本任务重构 Agent loop、工具系统、会话交付或后台任务架构。

## 4. 总体架构

采用双层日志架构：

### 4.1 用户可见运行事件

`RunEvent` 继续作为 SQLite 中的持久运行时间线，并由同一个公共投影器生成 SSE 和 API 输出。它只保存或公开用于理解运行状态的安全字段。

### 4.2 本地结构化诊断日志

本地 JSONL 文件保存开发排障信息。每条记录带统一关联字段，异常记录可包含经过脱敏的堆栈。文件日志不通过浏览器 API 暴露。

### 4.3 数据流

```text
AgentRuntime._publish()
        |
        +-- 公共安全投影
        |      +-- SQLite RunEvent
        |      +-- SSE broker
        |      +-- GET /api/runs/{run_id}/events
        |      +-- 前端诊断视图
        |
        +-- 结构化文件日志
               +-- 统一关联字段
               +-- 脱敏异常堆栈
               +-- 按天/大小轮转
               +-- 14 天清理
```

## 5. 模块职责

新增 `backend/src/observability/`：

- `context.py`：使用 `contextvars` 保存和传播 `trace_id`、`run_id`、`turn_id`、`parent_run_id`、`step`、`tool_call_id` 等运行上下文。
- `redaction.py`：提供字段脱敏、文本长度限制、命令摘要和路径相对化。
- `logging.py`：初始化 JSON formatter、控制台 handler、文件轮转 handler 和过期文件清理。
- `__init__.py`：公开稳定的初始化、上下文和日志辅助接口。

现有领域模块继续拥有业务事件语义：

- `agent/engine.py` 决定模型步骤、工具调用和完成验证事件；
- `runs/lifecycle.py` 决定 Run 生命周期、持久化、恢复、委派和终态交付；
- `tasks/background.py` 决定后台任务状态；
- `api/routes/shared.py` 拥有公共事件投影；
- `runs/stream.py` 只负责低延迟进程内投递，不承担可靠持久化。

可观察性模块不改变这些所有权，只提供统一记录能力。

## 6. 关联模型

### 6.1 Trace 来源

- 根 Run 优先复用 `ConversationTurn.trace_id`；
- 子 Run 继承父 trace，并记录自己的 `run_id` 和 `parent_run_id`；
- 没有 Turn 的启动恢复、后台维护或独立系统任务生成自己的 trace；
- 后台线程、线程池和子 asyncio task 必须显式复制或绑定上下文。

### 6.2 RunEvent 顺序

`RunEvent` 增加：

- `trace_id: str | None`；
- `sequence: int`，在同一 Run 内单调递增。

查询按 `sequence` 排序，而不是只依赖 `created_at`。这解决并发工具和跨线程事件时间相同或提交顺序不确定时的重放顺序问题。

### 6.3 统一字段

固定字段：

- `timestamp`
- `level`
- `logger`
- `message`
- `process_id`
- `thread_id`

运行字段：

- `trace_id`
- `run_id`
- `turn_id`
- `parent_run_id`
- `step`
- `tool_call_id`
- `tool_name`
- `event_type`
- `background_job_id`
- `child_run_id`

诊断字段：

- `phase`
- `status`
- `duration_ms`
- `error_code`
- `error_type`
- `retry_attempt`
- `retryable`
- `exception`，仅写入文件日志且必须先脱敏。

## 7. 事件覆盖

每个模型循环至少覆盖：

- `model_step_started`
- `model_step_finished`
- `model_retry_scheduled`
- `model_failed`
- `tool_started`
- `tool_finished`
- `approval_requested`
- `run_completed`
- `run_stopped`
- `integration_failed`

已有上下文压缩、完成验证、MCP、后台任务和委派事件继续保留，并补齐关联字段。事件只描述事实，不写入完整输入输出。

工具开始和结束事件记录：

- 工具名称和调用 ID；
- 参数键集合、参数字符数和安全摘要；
- 是否成功、是否改变状态、错误码和耗时；
- 如适用，审批 ID、后台 Job ID 或子 Run ID。

模型步骤记录：

- step、模型连接/模型标识的非凭据部分；
- 请求耗时、流式活动、重试次数、用量统计；
- 稳定错误码、异常类型和是否可重试。

## 8. 错误处理

1. 业务失败继续通过现有 `Run.status`、`error_code`、`stop_reason` 和终态交付处理。
2. 模型、工具、恢复、委派、后台任务和终态交付的未处理异常使用 `logger.exception(...)` 写入文件日志。
3. UI 和公开事件只接收稳定错误码、异常类型、阶段和简短公共说明。
4. 文件 logger 失败时向 stderr 输出一次明确告警，不触发模型重试或工具重放。
5. 持久事件 sink 失败时保留当前 `event_sink_failed` 语义，并确保 Run 最终进入可诊断的 integration failure，而不是长期停留在 `acting`。
6. 日志系统不得把失败伪装为成功，也不得吞掉原始业务异常。

## 9. 脱敏与安全边界

### 9.1 字段脱敏

字段名包含以下语义时，值替换为 `[REDACTED]`：

- `api_key`
- `token`
- `password`
- `secret`
- `authorization`
- `cookie`
- `headers`

文本层同时识别 Bearer/JWT、连接串密码、常见凭据环境变量和 URL query 凭据。

### 9.2 内容限制

- 不记录完整模型 prompt/response；
- 不记录工具结果正文、文件内容和补丁正文；
- 不记录 stdout/stderr；
- 命令只记录可执行文件名、参数数量和工作目录相对路径；
- 审批只记录参数键、类型和长度；
- 路径优先转换为工作区相对路径；
- 所有可变文本字段都有明确长度上限。

### 9.3 统一公开投影

SSE、历史事件 GET、事件 POST 响应、审批、后台任务、协作事件和委派结果统一使用公共安全投影。禁止 API 或前端在安全字段缺失时回退到原始 payload、原始参数、stdout/stderr 或异常文本。

## 10. 文件轮转与保留

默认配置：

```text
PGAGENT_LOG_LEVEL=INFO
PGAGENT_LOG_DIR=data/logs
PGAGENT_LOG_RETENTION_DAYS=14
PGAGENT_LOG_MAX_BYTES=20971520
```

文件命名：

```text
data/logs/pgagent-2026-09-07.jsonl
data/logs/pgagent-2026-09-07.1.jsonl
```

行为：

- 日期变化时创建新文件；
- 当日文件达到 20 MB 时创建递增分片；
- 应用启动时执行一次过期清理，运行期间每日最多执行一次；
- 删除修改时间超过 14 天且名称符合 PGAgent 日志命名规则的文件；
- 只清理日志目录内由本处理器创建的文件；
- 日志目录不可写、轮转失败或磁盘不足时记录 stderr 告警，不阻塞 Agent。

## 11. API 与 UI

### 11.1 API

不新增读取文件日志的接口。扩展现有：

```text
GET /api/runs/{run_id}/events
```

可选查询参数：

- `event_type`
- `step`
- `errors_only`
- `before`
- `limit`

返回安全字段包括事件时间、序号、步骤、事件类型、状态、耗时、工具名、工具调用 ID、错误码、错误类型、子 Run/后台 Job 关联和安全摘要。

`POST /api/runs/{run_id}/events` 的响应也必须经过相同投影，不能原样返回 payload。

### 11.2 UI

`RunsPage` 的 Run 详情新增“诊断”视图：

- 按步骤、事件类型和错误状态筛选；
- 展示事件时间、耗时、工具、错误码和重试；
- 支持跳转到关联子 Run 或后台 Job；
- 使用游标加载更多，不一次拉取全部事件；
- 不展示文件日志路径、异常堆栈或敏感原文。

Session 页面和 thought timeline 继续显示面向用户的运行进度，但必须复用相同的安全字段，不回退到原始内容。

## 12. 修改范围

后端主要涉及：

- `backend/src/observability/*`
- `backend/src/config/settings.py`
- `backend/src/main.py`
- `backend/src/persistence/models.py`
- `backend/src/persistence/database.py`
- `backend/src/runs/lifecycle.py`
- `backend/src/runs/runtime_factory.py`
- `backend/src/runs/stream.py`
- `backend/src/agent/engine.py`
- `backend/src/model/retry.py`
- `backend/src/tools/registry.py`
- `backend/src/tasks/background.py`
- `backend/src/agents/collaboration.py`
- 相关 MCP、memory、API route 和 schema 文件

前端主要涉及：

- `frontend/src/types.ts`
- `frontend/src/api.ts`
- `frontend/src/features/runs/RunsPage.tsx`
- `frontend/src/features/sessions/presentation.tsx`
- `frontend/src/features/sessions/SessionsPage.tsx`
- `frontend/src/thoughtTimeline.ts`
- 对应样式和测试文件

只修改实现本设计所需的代码，不处理无关的旧代码、格式或重构。

## 13. 验证与验收

### 13.1 后端单元测试

- JSON formatter 字段和上下文注入；
- 密钥、Bearer、连接串、审批参数和命令参数脱敏；
- 按天/大小轮转与 14 天清理；
- 事件 sink 同时产生 `RunEvent` 和文件日志；
- logger 失败不阻塞 Agent；
- 同一 Run 的 `sequence` 在并发工具调用下单调且无重复。

### 13.2 后端集成测试

- 普通成功 Run；
- 模型重试后成功；
- 模型最终失败；
- 工具失败；
- 审批等待和拒绝；
- 后台任务等待和恢复；
- 子 Agent 委派；
- 进程重启恢复；
- SSE、GET、POST、审批、后台任务和协作事件均不泄露敏感内容。

### 13.3 前端测试

- 诊断视图筛选、分页和错误状态；
- 不显示空思考内容；
- 不回退渲染原始参数、stdout/stderr 或完整异常；
- SSE 事件与历史事件显示一致。

### 13.4 验收命令

```text
cd backend
pytest -q

cd ../frontend
npm test -- --run
npm run build

cd ..
git diff --check
```

完成自动测试后执行一个真实本地 Run，至少覆盖一次模型步骤、一次工具调用和一次失败路径，并核对 UI 时间线、SQLite `RunEvent` 与 `data/logs/*.jsonl` 的关联字段一致。

## 14. 已知风险

- 工具密集型 Run 会增加日志量；通过摘要记录、20 MB 分片和 14 天清理控制磁盘占用。
- 内容模式无法证明识别所有自定义秘密格式；字段名脱敏是第一层，内容模式是补充层，UI/API 继续采用严格白名单。
- `RunEvent.sequence` 的分配涉及 SQLite 并发写入，需要迁移和并发测试验证。
- 日志 handler 自身发生 I/O 故障时必须保持错误可见，同时不能改变 Agent 的业务结果。
- 当前工作树已有大量未提交修改；实施和提交必须显式限定文件，不覆盖或夹带无关内容。

## 15. 完成标准

满足以下条件才视为完成：

1. 每个 Agent 模型步骤和工具调用都有可关联的开始/结束或失败记录；
2. 任一 Run 可通过 `trace_id/run_id/step/tool_call_id` 从 UI 事件定位到本地诊断日志；
3. 关键失败路径包含脱敏异常堆栈；
4. SSE、API 和 UI 不暴露禁止记录的原始内容；
5. 轮转、保留、并发事件顺序和日志故障路径通过测试；
6. 后端测试、前端测试、前端构建和 diff 检查通过；
7. 真实本地 Run 的三层关联验收通过。
