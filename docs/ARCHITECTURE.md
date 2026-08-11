# PGAgent v0.1 架构契约

PGAgent 是单机、单用户、本地优先的 Agent 工作台。前端采用 React + Vite + TypeScript，后端采用 FastAPI，运行数据与 LangGraph checkpoint 分别保存在两个 SQLite 文件中。

## 固定目录

- `data/pgagent.db`：工作区、会话、消息、模型连接、运行、审批、记忆、Token 用量、多 Agent 任务和通信。
- `data/langgraph_checkpoints.db`：LangGraph checkpoint。
- `data/workspaces/{workspace_id}/`：工具可以访问的工作目录。所有路径必须经过 resolve 后仍位于对应工作区。

## 外层状态机

`received -> preparing_context -> acting -> awaiting_approval -> observing -> completed|failed|stopped`

模型以 `auto` 模式自行判断任务是否需要内部规划。外层运行时负责重复调用、无进展、超时、取消、审批与 checkpoint，不再用任意的总步数或总工具次数截断长任务。

## 工具与审批

- 只读：`list_files`、`read_file`、`search_files`、`get_current_time`。
- 有副作用：`write_file`、`run_command`，默认需要用户批准。
- 命令工具使用允许列表、工作区 cwd、超时和输出上限；禁止 shell 链式危险命令。

## 上下文

上下文由系统提示、Agent 配置、工作区规则、会话摘要、固定记忆、最近消息和按需工具结果组成。单个会话预算为 100k Token，达到 90k 时压缩旧消息；压缩游标只会推进到已纳入摘要的完整消息区间。会话、工作区和全局记忆分层保存。

## 失败与防循环

- 总执行步数和总工具调用次数不设硬上限。
- 活跃执行时间默认最多 1800 秒并跨审批累计；审批等待时间不计入。该保险丝独立于步数/工具次数，用于兜住参数持续变化的异常循环。
- 同一工具与规范化参数连续出现 3 次即停止。
- 连续 4 步没有新文件、任务状态或有效输出即停止。
- 401/403 和无效 API Key 不重试；429、连接失败和 5xx 采用有抖动的指数退避，最多 3 次。
- 所有停止原因写入运行事件，允许用户从最后 checkpoint 继续或修改配置后重试。

## 多 Agent 通信

SQLite `team_tasks` 是唯一任务真相源；`agent_messages` 保存结构化信封。消息类型包括 `TASK_ASSIGNED`、`PLAN_SUBMITTED`、`PROGRESS`、`ARTIFACT_READY`、`BLOCKED`、`REVIEW_REQUESTED`、`REVIEW_RESULT` 和 `TASK_COMPLETED`。任务认领使用 `version` CAS 和带过期时间的 lease，发送端使用幂等键；每个 worker 使用独立工作目录和独立上下文，lead 负责拆解、验收和汇总。

## 模型网关

模型连接最少只需 Base URL 与 API Key。保存时先请求 OpenAI 兼容的 `/models`，失败时允许手动模型 ID；随后执行轻量能力探测。API Key 进入 Windows Credential Manager，SQLite 仅保存引用。LiteLLM 负责统一调用、Token 用量和可识别模型的成本估算，provider adapter 负责 URL、headers、模型名与 `off/auto/low/medium/high/xhigh` 思考强度映射。

## v0.1 API 前缀

- `/api/health`
- `/api/dashboard`
- `/api/workspaces`
- `/api/agents`
- `/api/connections`
- `/api/sessions`
- `/api/runs`
- `/api/approvals`
- `/api/memories`
- `/api/teams`
- `/api/usage`
- `/api/system/select-folder`
