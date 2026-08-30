# PGAgent v0.1 架构契约

PGAgent 是单机、单用户、本地优先的 Agent 工作台。前端采用 React + Vite + TypeScript，后端采用 FastAPI，业务和可恢复运行状态统一保存在应用 SQLite 中。

## 固定目录

- `data/pgagent.db`：工作区、会话、消息、模型连接、运行、审批、记忆、Token 用量和会话级子 Agent 委派记录。
- `data/workspaces/{workspace_id}/`：工具可以访问的工作目录。所有路径必须经过 resolve 后仍位于对应工作区。
- `data/skills/{slug}/`：由 Skill registry 管理的 Skill 源文件副本；导入流程只复制和解析，不自动执行其中的脚本、命令或网络请求。
- `data/mcp.json`：显式启用的 MCP stdio/Streamable HTTP server 配置。配置中的 `${ENV_NAME}` 只在内存展开，不进入运行快照或状态 API。

## 项目、会话与主控

工作区在产品界面中称为“项目”：目录路径规范化后作为项目唯一标识依据，重复选择同一目录会复用已有项目。默认工作区不显示在项目树中，它承载没有选择目录的一次性任务。

新建对话先存在于前端草稿状态，不写 SQLite；当用户从某个项目会话发起新对话时，草稿仅在前端预选该项目目录，仍可重新选择或清除。第一次发送通过 `POST /api/drafts/launch` 的单个事务创建（或复用）项目、以首条内容生成会话标题，并原子创建会话、`ConversationTurn`、用户消息、根运行和幂等记录，提交后才启动运行。浏览器保留草稿级幂等键；同键同内容重试只返回原会话与原运行，不会二次启动模型，同键不同内容会明确返回冲突。后续每条会话消息同样携带客户端幂等键。离开未发送的草稿不会留下会话、消息、运行或 Token 用量。

每个会话固定绑定 `DEFAULT_AGENT_ID`，即不可编辑、不可删除的 PGAgent 主控。主控负责意图识别、复杂度判断、规划、执行结果汇总和继续/输出决策。已启用的用户创建 Agent 会作为可委派子 Agent 的安全目录项提供给主控；主控可通过 `task(agent_id, task)` 委派单个任务，也可通过 `task(tasks=[...])` 提交带显式 `id/depends_on` 的任务图。没有依赖关系的节点在同一波并发执行，后继节点仅在全部前置节点成功后解锁。

## 对话交付保证

`ConversationTurn` 是一次已接收用户输入的持久化交付回执，不是上下文任务目标。一个 Turn 可以在内部拆成多个目标、工具步骤和子 Run，但数据库通过唯一终态键保证它最终至多写入一条用户可见 assistant 终态消息；正常模型输出、失败、停止、审批拒绝、空输出、调度失败和重启中断都通过同一确定性收口函数写入。模型没有可用输出时由本地模板生成说明和追踪号，不调用额外模型或 evaluator。`execution_status` 可为 `partial_failure`，而 `reply_status=delivered` 仅表示系统已经向用户交代清楚，两者不能混为一谈。

子 Agent 只把结构化结果写回 `delegated_tasks`，不直接占用主会话的终态回复。主控正常时负责自然语言汇总；若主控在汇总阶段失败，系统会根据每个子任务的完成、失败、停止及 `workspace_changed` 记录生成基础汇总，并提醒可能存在的部分修改。进程启动和后台 watchdog 会修复终态但缺少回复的历史/异常 Run，并结束没有协调器任务的孤儿根 Run。SSE 仅用于实时显示，前端断流后按 `run_id/turn_id` 重新读取持久化消息。

## 持久化任务计划与恢复

`ConversationTurn` 负责一轮消息的可靠交付，`DurableTask` 则负责跨多轮、跨进程保存用户目标。主控对包含两个以上可核验步骤的工作调用 `todowrite` 后，后端会同步创建一条 `durable_tasks`、`plan_steps` 和 `plan_step_dependencies`；每一步使用跨更新保持不变的显式 ID，并保存状态、执行器类型（主 Agent、子 Agent 或后台 Job）、目标 Agent/Run、工作区模式、尝试次数、结果与证据。SQLite `BEGIN IMMEDIATE` 保护“检查依赖并认领”这一原子边界，保证并发执行器不会重复认领同一步。`Run.task_id/plan_step_id` 记录本次运行归属，`run_kind/resumed_from_run_id` 记录初次执行或恢复链路。兼容的 `TaskCreate/task_create/TaskUpdate/task_update/claim_task` 也使用这套表，不再在真实运行中维护第二份 `tasks.json`。任务面板通过 `/api/sessions/{id}/active-task` 显示依赖、执行器和进度，完整历史可由 `/api/sessions/{id}/tasks` 获取。

用户主动停止时，任务转为 `paused`；进程重启会把遗留的活动 Run 收口为 `stopped/interrupted_restart`，任务和当时的活动步骤转为 `needs_recovery`。用户随后输入“继续刚刚的工作”等明确续接语句时，新 Run 会绑定该会话最近的未完成任务，并收到后端生成的恢复包。恢复包是结构化事实来源：模型不得重做已完成步骤，对 `needs_recovery` 步骤必须先检查文件、测试或其他工作区事实，再决定标记完成还是补做。每次成功的 `todowrite` 都会重新落盘进度，所以恢复不依赖模型从整段聊天历史中猜测做到哪里。

浏览器单独断网不会立即中止后端 Run；重新连接后前端继续按持久化 Run、消息和任务状态同步。如果后端进程同时退出，则以上启动收口与恢复包流程生效。任务粒度是语义步骤，不是任意工具调用级事务，因此中断发生在写文件或外部副作用中间时，系统只保证标记为待核验，不会假定该动作成功，也不会盲目自动重放。

## 外层状态机

`received -> preparing_context -> acting -> awaiting_approval -> observing -> verifying -> completed|failed|stopped`

主 Agent 给出候选答复后必须经过本地确定性完成门禁。门禁使用显式的当前 Run 轨迹检查非空答复、最终 assistant 对齐、工具调用 ID 唯一、每次调用恰有一个同名结构化结果、计数一致且没有待审批项。该过程不调用第二个模型，也不做主观语义评分；普通无工具回答只产生主 Agent 自身的一次模型请求。确定性检查拒绝时，反馈只在内存中回灌主 Agent 继续修订，不写入用户会话；默认最多 3 次，全部失败后以 acceptance_failed 停止。只有通过后候选文本才作为成功答复发布；失败或停止则由对话交付层持久化确定性终态说明。等待用户澄清使用 needs_user_input，不冒充任务完成。

模型以 `auto` 模式自行判断任务是否需要内部规划。`AgentRuntime` 的显式循环负责重复调用、无进展、超时和完成验证；`RunCoordinator` 负责取消、审批和基于应用运行快照的恢复，不再用任意的总步数或总工具次数截断长任务。

## 工具与审批

主控运行时使用公开工具名 `bash/read/write/edit/apply_patch/validate/review_finding/debug_evidence/glob/grep/rg/webfetch/websearch/task/todowrite/question/skill`。`ToolRegistry` 只注册该次运行由 `Agent.tool_ids` 选择的名称，因而未选择工具不会进入 provider 的 function schema；该选择、权限模式、工作流 profile、已选 Skill 指令、待办和工程证据会写入 `runtime_binding`，审批恢复优先使用冻结值而非会话后来的修改。`Agent.workflow_profile_id` 可显式选择 `general/coding/review/debug`；`auto` 仅保留旧版“同时具备 apply_patch 与 validate 时启用 coding”的兼容行为。工程 profile 同时具备 `ToolSearch` 时，各自核心工具直接暴露，其他已授权内置工具以 deferred 形式保留，通过 `select:<tool-name>` 按需进入后续模型轮次。

Review profile 额外形成只读工作流上限：即使 Agent 配置误选了写入、命令执行或委派工具，这些工具也会在本 Run 的模型暴露层隐藏，且不能通过 `ToolSearch` 激活；需要修改代码时必须切换到 Coding 或 Debug profile。该限制不新增第二套运行时，底层仍复用相同的注册、审批、沙箱和恢复链路。

- 文件工具通过 `WorkspaceSandbox` 解析真实路径并拒绝目录逃逸、Junction 和符号链接越界。`read` 支持按真实行范围读取；`edit` 只做精确文本替换，默认要求唯一匹配；`apply_patch` 在写入前验证全部文件和 hunk，在目标同目录暂存替换内容，后续提交失败时回滚已应用文件，并保留原文件换行风格；`glob`/`grep` 保留兼容行为，`rg` 提供一等的 ripgrep 代码检索，并限制路径、文件大小和结果量。
- `bash` 从不启动 shell，只允许裸 allowlist 可执行文件，支持工作区内的显式 `cwd`、超时、进程树终止和输出上限。`validate` 复用相同边界并记录测试、lint、类型检查或构建的结构化结果。`full` 仅跳过审批，不会取消 allowlist 或工作区边界。
- `webfetch` 使用无环境代理的 HTTP 客户端，只允许公开 HTTP(S) DNS 地址，拒绝私网/回环/保留地址与自动重定向，并限制响应大小和超时；`websearch` 通过 DuckDuckGo HTML 返回真实解析结果，失败时显式返回 provider 不可用。
- `todowrite` 是 JSON 可序列化的运行/会话待办状态；`skill` 只按 ID 返回会话已选的受管理 `SKILL.md` 文本，不执行脚本；`question` 结束本轮并将澄清问题作为正常助手消息。`task` 会先持久化完整 DAG，再按 ready wave 并发创建幂等委派记录和独立子 `Run`；失败前置节点的后继任务不会被错误启动。
- 子 Agent 继承父运行已经冻结的权限模式；它的工具为“父运行允许工具”与“子 Agent 自己勾选工具”的交集，并强制移除递归委派、团队管理和共享任务板写入工具。子 Run 若需要二次审批会真实进入 `awaiting_approval`，若其后台 Job 未完成则进入事件等待，绝不把未批准或仅入队的操作说成已完成。
- `ask`：写入、命令、委派与联网均须批准；`smart`：低风险读取和受限公网读取自动执行，写入、命令和委派须批准；`full`：无需批准，但仍保留上述基础安全边界。
- 事件流提供 `model_step_started`、`tool_started`、`tool_finished` 和终态事件，携带安全参数摘要和耗时；工具结果正文、写入正文、Token/API Key 不写入时间线事件。

### MCP 工具运行时

启用内置 `MCP` 能力后，`McpRuntimePool` 以 PGAgent Session 为统一清理边界，并以 `(session, runtime scope, resolved workspace root)` 区分连接集合。主 Agent 的 scope 是会话，使用 `eager` 策略；每个委派 SubAgent 以自己的 Run ID 持有独立 scope，使用 `lazy_when_cached`，不会与父 Agent 或其他 SubAgent 共享连接状态。stdio server 由 SDK 作为子进程启动，Streamable HTTP 使用同一异步 Client 接口且不跟随 HTTP 重定向。每个 server 独立完成协议协商和 `tools/list`；required server 在没有缓存且启动失败时阻止本次运行，可选 server 失败只记录状态。目录刷新失败时保留该连接最后一次成功发现的工具，并标记为 degraded。

`McpRuntimePool` 持有独立于连接的进程级 Tool Catalog Cache，采用 32 项 LRU 和 30 分钟 TTL。目录身份包含 server 名称、展开后的连接配置和工作区；SubAgent 命中非空、已通过 server tool filter 的缓存目录时，以 `dormant` 状态发布，不创建 stdio 子进程或 HTTP 连接。主 Agent 始终 eager；SubAgent 在缓存缺失、过期、配置身份变化或目录为空时立即启动。required server 有合格缓存时同样可以休眠，没有缓存时仍按 fail-closed 语义验证启动。休眠连接只会在具体工具或资源调用时由对应 server 的 startup lock 唤醒；并发调用共享一次启动。握手后的 live catalog 若与本轮缓存不同，会更新缓存并拒绝按旧 schema 执行。

发现结果保留原始 `(server_name, tool_name)` 路由身份，另生成符合模型函数名约束且跨 server 唯一的 `mcp__server__tool` 名称。`ToolRegistry` 将可执行注册与模型暴露分离：通用 `MCP` 路由保持隐藏，`McpToolSearch` 直接可见，具体 MCP 工具以 deferred 状态注册；搜索按 server、原始名称、模型名和描述排序，命中项在下一模型轮次加入 function schema。系统提示只公布可搜索 namespace 和工具数量，不展开完整 schema。Agent loop 仍不区分已加载的本地工具和 MCP 工具。运行事件将目录准备、缓存休眠、按需连接和单 server 就绪分开记录。

完整工具定义、输入 schema、只读标记、模型名和本 Run 已激活的 deferred 工具写入 `runtime_binding`；审批、后台等待或恢复时重建相同暴露集合，若目录定义变化则拒绝让旧 Run 继续，避免已批准调用落到不同能力。`readOnlyHint=true` 只影响审批与安全并行资格，不改变 MCP server 自身权限。

所有连接在会话删除或应用 shutdown 时统一关闭。`GET /api/mcp` 状态接口只返回传输类型、状态和稳定错误类型，不序列化 command、Header、环境变量值或原始传输异常；工作台使用独立的 `/api/mcp/servers` 配置接口读写用户明确管理的名称、STDIO command、args 与 enabled。配置写入以临时文件替换完成：仍有 Agent 运行、审批或后台等待时返回 409；空闲时由 `McpRuntimePool` 的配置代次锁串行化持久化与连接启动，淘汰旧缓存连接，启动途中若代次变化则丢弃旧快照并以新配置重试。当前版本只实现 MCP Client，不提供控制 PGAgent 的 MCP Server 接口。

## 能力目录、Skill 与权限配置

`GET /api/tools` 返回稳定的内置目录项，包括 `bash`、`read`、`write`、`edit`、`apply_patch`、`validate`、`review_finding`、`debug_evidence`、`glob`、`grep`、`rg`、`webfetch`、`websearch`、`task`、`todowrite`、`question`、`skill` 以及兼容工具。每项都含 `availability`、风险等级、审批要求和（如有）运行时工具映射；前端必须以这些字段为准，不能把目录存在误解为已允许执行。

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
- 所有停止原因写入运行事件，允许用户从最后运行快照继续或修改配置后重试。

## 子 Agent 委派

SQLite `delegated_tasks` 保存父会话、父 Run、子 Run、任务图步骤、持久化队友和结构化结果。`task` 采用单层、有依赖的并行委派：一项父工具调用最多创建 8 个子 Run；同一 ready wave 通过异步任务并发执行，下一 wave 等待其声明的前置节点完成，避免无界 fan-out 和错误串行。每次直接委派创建的一次性子运行在任务完成后结束；它不是常驻进程。

需要跨多次分配保留角色、提示词、历史结果和收件箱时，主控使用 `spawn_teammate` 创建 `collaboration_teams/teammate_workers` 中的持久化队友身份。持久化的是身份和协作上下文，不是永久占用线程的 LLM 进程；每次分配仍创建一个新的子 Run，但会注入该队友的未读消息和最近任务结果。`send_message/read_inbox/broadcast` 使用 SQLite `collaboration_messages`，子 Agent 可向 `lead` 回信；终态和消息通知写入 `collaboration_events`。状态可通过 `/api/sessions/{id}/teammates`、`collaboration-messages` 和 `collaboration-events` 查询。

队友默认共享父工作区。`workspace_mode=worktree` 会为该队友创建独立 Git branch/worktree，子 Run 的沙箱根切换到该 worktree，避免多个写入型 Agent 同时修改同一目录。创建流程先短事务写入 `provisioning`，再在事务外执行 Git，成功后用第二个短事务转为 `idle`，所以大型仓库或 Git hook 不会长期占用 SQLite 写锁。主控通过需要审批的 `integrate_teammate` 显式提交并合并队友分支；队友仍在工作时拒绝合并，冲突或超时时执行 `merge --abort` 并返回明确错误。

长时间下载、安装和构建使用持久化 `background_jobs`。`background_run` 只负责入队并立即返回 Job ID，命令由独立后台线程执行，状态、PID、超时、日志路径和终态结果写入 SQLite；需要交互的运行中命令可由 `write_stdin` 发送输入或关闭 stdin。传入 `plan_step_id` 时 Job 会原子认领并在终态结算对应 DAG 步骤。Agent 可在入队后继续处理不依赖该结果的 ready step。若准备结束时仍有活动 Job，完成验收把 Run 持久化为 `stopped/waiting_background` 并登记 `waiting_run_id`，不再让 LLM 循环调用“完成了吗”。后台线程只在终态写一次事件并通知协调器；协调器确认该 Run 的全部等待 Job 均终态后自动恢复模型，启动 watchdog 也会重放遗漏的通知。终态事件先作为未确认消息注入模型，只有包含处理结果的 Run outcome 成功提交时才在同一事务中写入 `observed_at/consumed_at`；provider 失败或进程退出会保留事件供下次至少一次重投。`check_background` 保留为人工查询兼容工具，不是自动续跑的必要条件。正常关闭时运行中的 Job 会终止并回到队列，下次启动自动恢复；异常退出后无法证明原进程终态的 Job 会明确标记失败，不会盲目重复安装命令。会话可通过 `/api/sessions/{id}/background-jobs` 读取持久状态。

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
- `/api/usage`
- `/api/system/select-folder`
