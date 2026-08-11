# PGAgent v0.1 架构契约

PGAgent 是单机、单用户、本地优先的 Agent 工作台。前端采用 React + Vite + TypeScript，后端采用 FastAPI，运行数据与 LangGraph checkpoint 分别保存在两个 SQLite 文件中。

## 固定目录

- `data/pgagent.db`：工作区、会话、消息、模型连接、运行、审批、记忆、Token 用量、多 Agent 任务和通信。
- `data/langgraph_checkpoints.db`：LangGraph checkpoint。
- `data/workspaces/{workspace_id}/`：工具可以访问的工作目录。所有路径必须经过 resolve 后仍位于对应工作区。
- `data/skills/{slug}/`：由 Skill registry 管理的 Skill 源文件副本；导入流程只复制和解析，不自动执行其中的脚本、命令或网络请求。

## 项目、会话与主控

工作区在产品界面中称为“项目”：目录路径规范化后作为项目唯一标识依据，重复选择同一目录会复用已有项目。默认工作区不显示在项目树中，它承载没有选择目录的一次性任务。

新建对话先存在于前端草稿状态，不写 SQLite；当用户从某个项目会话发起新对话时，草稿仅在前端预选该项目目录，仍可重新选择或清除。第一次发送通过 `POST /api/drafts/launch` 的单个事务创建（或复用）项目、以首条内容生成会话标题、创建会话、用户消息、运行和幂等记录，提交后才启动运行。浏览器保留草稿级幂等键；同键同内容重试只返回原会话和原运行，不会二次启动模型，同键不同内容会明确返回冲突。离开未发送的草稿不会留下会话、消息、运行或 Token 用量。

每个会话固定绑定 `DEFAULT_AGENT_ID`，即不可编辑、不可删除的 PGAgent 主控。主控负责意图识别、复杂度判断、规划、执行结果汇总和继续/输出决策。已启用的用户创建 Agent 会作为可委派子 Agent 的安全目录项提供给主控；主控只能通过 `task(agent_id, task)` 选择其中一个精确 ID，不会伪造未执行的委派。

## 外层状态机

`received -> preparing_context -> acting -> awaiting_approval -> observing -> completed|failed|stopped`

模型以 `auto` 模式自行判断任务是否需要内部规划。外层运行时负责重复调用、无进展、超时、取消、审批与 checkpoint，不再用任意的总步数或总工具次数截断长任务。

## 工具与审批

主控运行时使用公开工具名 `bash/read/write/edit/glob/grep/webfetch/websearch/task/todowrite/question/skill`。`ToolRegistry` 只注册该次运行由 `Agent.tool_ids` 选择的名称，因而未选择工具不会进入 provider 的 function schema；该选择、权限模式、已选 Skill 指令和待办状态会写入 `runtime_binding`，审批恢复优先使用冻结值而非会话后来的修改。

- 文件工具通过 `WorkspaceSandbox` 解析真实路径并拒绝目录逃逸、Junction 和符号链接越界。`edit` 只做精确文本替换，默认要求唯一匹配；`glob`/`grep` 有扫描、文件大小和结果上限。
- `bash` 从不启动 shell，只允许裸 allowlist 可执行文件，使用工作区 cwd、超时、进程树终止和输出上限。`full` 仅跳过审批，不会取消 allowlist 或工作区边界。
- `webfetch` 使用无环境代理的 HTTP 客户端，只允许公开 HTTP(S) DNS 地址，拒绝私网/回环/保留地址与自动重定向，并限制响应大小和超时；`websearch` 通过 DuckDuckGo HTML 返回真实解析结果，失败时显式返回 provider 不可用。
- `todowrite` 是 JSON 可序列化的运行/会话待办状态；`skill` 只按 ID 返回会话已选的受管理 `SKILL.md` 文本，不执行脚本；`question` 结束本轮并将澄清问题作为正常助手消息。`task` 会创建一个幂等的 `TeamTask`、`TASK_ASSIGNED` 消息和独立子 `Run`，同步等待子运行结果，再把有状态、run/task ID、工具计数和截断输出的结构化结果交回主控。
- 子 Agent 继承父运行已经冻结的工作区和权限模式；它的工具为“父运行允许工具”与“子 Agent 自己勾选工具”的交集，强制移除 `task`，并冻结自身的模型、Skill 和提示词。子 Run 若需要二次审批会真实进入 `awaiting_approval`/`blocked`，绝不把未批准操作说成已完成。
- `ask`：写入、命令、委派与联网均须批准；`smart`：低风险读取和受限公网读取自动执行，写入、命令和委派须批准；`full`：无需批准，但仍保留上述基础安全边界。
- 事件流提供 `model_step_started`、`tool_started`、`tool_finished` 和终态事件，携带安全参数摘要和耗时；工具结果正文、写入正文、Token/API Key 不写入时间线事件。

## 能力目录、Skill 与权限配置

`GET /api/tools` 返回稳定的内置目录项：`bash`、`read`、`write`、`edit`、`glob`、`grep`、`webfetch`、`websearch`、`task`、`todowrite`、`question`、`skill`。每项都含 `availability`、风险等级、审批要求和（如有）运行时工具映射；前端必须以这些字段为准，不能把目录存在误解为已允许执行。

`skills`、`agent_tools`、`agent_skills` 和 `session_skills` 是独立 SQLite 表。用户 Agent 的 `tool_ids`/`skill_ids` 通过关系表保存；Session 的 `skill_ids` 通过关系表保存，`permission_mode` 为 `ask | smart | full`。`AgentRead`、`SessionRead` 均返回稳定的 ID 数组。固定 `DEFAULT_AGENT_ID` 每次初始化都会恢复完整内置 Tool 目录，且资源 API 拒绝对其修改或删除。

Skill registry 仅管理本地副本和元数据：`SKILL.md` 必须为 UTF-8，frontmatter/首段描述会被安全解析；符号链接、目录穿越、超大文件和超限文件数会被拒绝。GitHub ZIP 会验证 HTTPS GitHub 主机、重定向、ZIP 路径和大小；市场/远程安装先返回文件预览，只有 `confirm=true` 才复制到 `data/skills`。导入或关联 Skill 不会执行其内容。

市场搜索使用 skills.sh 官方 `/api/v1/skills/search` 和 detail/files 接口，令牌来自 `SKILLS_SH_API_TOKEN`（也兼容 `PGAGENT_SKILLS_SH_API_TOKEN` 或 `VERCEL_OIDC_TOKEN`）。没有令牌时 API 返回 `available=false`，而不是伪造公开 marketplace 数据。运行时会把已选工具、Skill 和 permission mode 放进会话运行配置快照；只有明确实现的工具执行层才可据此授权实际操作。

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

SQLite `team_tasks` 是唯一任务真相源；`agent_messages` 保存结构化信封。消息类型包括 `TASK_ASSIGNED`、`PLAN_SUBMITTED`、`PROGRESS`、`ARTIFACT_READY`、`BLOCKED`、`REVIEW_REQUESTED`、`REVIEW_RESULT` 和 `TASK_COMPLETED`。`task` 当前采用单层同步委派：一项父工具调用最多创建一个子 Run，避免递归或无界 fan-out；子 Agent 使用父会话的冻结工作区而不是其个人工作区。任务认领使用 `version` CAS 和带过期时间的 lease，发送端使用幂等键；lead 负责拆解、验收和汇总。

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
- `/api/tools`
- `/api/skills`
- `/api/skills/import`
- `/api/skills/market/status`
- `/api/skills/market/search`
- `/api/skills/market/install`
- `/api/teams`
- `/api/usage`
- `/api/system/select-folder`
