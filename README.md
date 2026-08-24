# PGAgent

> 主 Agent 的候选结果在发送给用户前只经过本地确定性运行链路检查，不会额外调用模型评分。检查失败会驱动主 Agent 修订，默认最多 3 次；未通过的候选不会写入聊天记录或发布 run_completed。确定性场景清单位于 backend/tests/acceptance_scenarios.json。

PGAgent 是一个本地优先、单用户的 Agent 工作台。它参考了视频中 TWork 的产品形态，以及 Claude Code 的 harness 思路：模型在一个简洁的工具循环里动态安排工作，外层由 LangGraph 管理阶段、checkpoint、人工审批和安全停止。

当前版本以“项目 + 会话”组织本地任务：可接入模型、选择本机目录作为项目、聊天执行任务、审批写文件/命令工具、统计 Token 与成本，并由固定主控按需调用独立上下文中的专业子 Agent。

## 已实现功能

- 总览、项目与会话、子 Agent、运行记录、用量统计、模型设置六个主要界面。
- 左侧会话栏按项目组织。项目是一个受沙箱保护的本地目录；默认工作区中的一次性任务显示在“任务”区。
- 点击“新建对话”只创建浏览器内草稿；首次发送通过单个原子、幂等请求以首条内容生成标题并写入项目、会话、消息和运行记录。网络中断后使用同一草稿重试只会返回原会话与原运行，不会重复调用模型。
- 每条被后端接收的用户消息都会原子创建一个 `ConversationTurn` 交付回执，并最终写入且只写入一条用户可见的终态回复。模型空输出、工具或集成异常、用户停止、审批拒绝、调度失败和进程重启也会得到带追踪号的确定性说明；该兜底不调用模型或 evaluator。子 Agent 只回传结构化结果，由主控或本地兜底统一汇总，不会越级向主会话重复回复。
- 所有会话由固定的“PGAgent 主控”处理意图识别、任务分析/编排、结果汇总与下一步决策。主控可通过 `task` 提交带 `id/depends_on` 的任务图：无依赖节点并发执行，后继节点在前置成功后自动解锁；每一步和依赖边均落入 SQLite。
- 用户创建且启用的 Agent 可用于一次性子 Run，也可由 `spawn_teammate` 绑定成持久化队友身份。队友在多次分配间保留角色提示、最近结果和 SQLite 收件箱；每次执行仍是独立、可追踪的子 Run，不会常驻占用一个 LLM 调用。
- 长时间下载、安装和构建可通过 `background_run` 挂入后台并关联任务图步骤。主/子 Agent 会先继续做其他 ready step；需要等待时 Run 进入事件等待，Job 终态由协调器自动唤醒，不需要 LLM 反复调用 `check_background`。
- 会话输入区可直接切换模型和思考强度，并显示当前上下文 Token 占用。
- OpenAI 兼容中转站、OpenRouter、DeepSeek 等连接；填写 Base URL 与 API Key 后自动请求 `/models`，也支持手动模型 ID。
- `off / auto / low / medium / high / xhigh` 思考强度，调用时由 provider adapter 映射。
- 主控的运行时工具为 `bash/read/write/edit/glob/grep/webfetch/websearch/task/todowrite/question/skill/git_status/git_diff/file_info`；每次运行只把当前 Agent 已选择的工具暴露给模型，并把该列表冻结到运行快照。后三个开发工具是受边界限制的只读能力，分别用于查看 Git 状态、Git 差异和文件元数据。
- `bash` 只运行工作区 cwd 中的 allowlist 命令，绝不启动 shell；`read/write/edit/glob/grep` 始终受工作区真实路径、符号链接和 Junction 边界保护。
- `webfetch` 仅访问公开 HTTP(S) 地址，拒绝私网/回环地址、环境代理、自动重定向和超大响应；`websearch` 使用公开 DuckDuckGo HTML，服务不可用时明确报错而不编造结果。
- `todowrite` 保存结构化待办并随运行快照恢复；`question` 会将本轮安全结束为一条澄清消息，等待用户下一条回复；`task` 会创建会话级幂等委派记录和独立子运行，完成后返回子 Agent、Run、状态、计数和输出摘要，不会假称已委派。
- 可导入本地 `SKILL.md` 文件夹到受管理的 `data/skills/`；Skill 的文件只会被复制和解析元数据，不会在导入、预览或关联 Agent/会话时执行。运行时只允许加载本会话选择的 Skill 文字指令，绝不自动执行其中的脚本。
- 权限模式：`ask` 对写入、命令、委派和联网均请求批准；`smart` 自动允许受限的只读/公网读取，写入、命令和委派请求批准；`full` 跳过批准，但不会绕过工作区、命令 allowlist 或 SSRF 防护。批准后从持久化快照精确恢复。
- 不限制任务的总执行步数和总工具调用次数；连续 3 次相同调用或连续 4 步无进展仍会安全停止。
- 单次任务的活跃执行时间默认有 30 分钟紧急保险丝（审批等待不计入），用于阻止无法识别的循环持续消耗费用；可通过环境变量调整。
- 401/403 不重试；429、5xx、连接失败和模型超时最多重试 3 次。
- 单个会话上下文预算为 100k Token，达到 90k 时自动压缩；会话、工作区、全局三级记忆继续分层保存。
- 用量页统计请求数、输入/输出/缓存 Token、缓存命中率、估算成本以及逐模型明细；无法识别价格的模型成本显示为 0。
- 运行记录详情会在有 provider 用量回报时展示本次运行的输入/输出 Token、缓存命中率和成本；没有回报时明确标记为不可统计，不把汇总数据冒充单次数据。
- 子 Agent 委派记录归属于父会话和父运行；子运行使用独立上下文，主控仅接收受限的结构化结果。写入型队友可选择独立 Git worktree，并由主控通过需审批的 `integrate_teammate` 显式合并。
- UI 视觉层采用独立的磨砂玻璃样式文件，支持浅色/深色主题、居中阅读列、固定输入栏、消息内联时间线和四个专业 Agent 快速模板；设计取舍与响应式约束见 `docs/UI_ARCHITECTURE.md`。

## 运行环境

- 项目目录：`C:\Users\xxr\Desktop\agent_test\PGAgent`
- Conda 环境：`E:\anaconda3\envs\agent_dock`
- Python：3.12
- 页面与 API：`http://127.0.0.1:8765`

当前机器的环境与依赖已经安装完毕。以后直接双击 `start_pgagent.bat`，脚本会启动本地服务并自动打开浏览器；在终端按 `Ctrl+C` 停止。

如需重新安装，双击 `setup_pgagent.bat`。安装脚本只写入上述独立 Conda 环境，不修改全局 Python；创建环境时临时绕开本机已经失效的 `pkgs/free` Conda 源，不会改写你的 `.condarc`。

## 第一次使用

1. 打开“模型设置”，新增连接。Base URL 通常应包含 `/v1`，例如 `https://api.deepseek.com/v1`；中转站以其说明为准。
2. 如果服务不支持 `/models`，在表单中填写手动模型 ID。
3. 打开“会话”，在“项目”标题右侧点击 `+`，使用 Windows 原生窗口选择允许 Agent 操作的本地目录；项目名称会自动采用目录名，不需要手输路径、名称或说明。
4. 点击侧栏最上方的“新建对话”开始草稿。如果当前打开的是某个项目下的会话，草稿会自动预选该项目目录；也可以在输入框左上角重新选择或清除目录。不选择目录时会作为默认工作区中的一次性任务。没有发送就离开草稿不会创建历史记录。
5. 如需为主控准备可复用的专业角色，可在“子 Agent”中创建带系统提示词的子 Agent，并可随时编辑或删除。主控不在此列表中，也不能被编辑或删除。
6. 发送任务后，PGAgent 主控会自行判断是否需要先规划；按当前权限模式调用已选择的工具。工具时间线只显示安全摘要、耗时与状态，不写入文件正文、工具返回正文或 API Key。

API Key 不写入 SQLite，保存到 Windows Credential Manager；数据库只保存 `secret_ref`。自定义 Header 不允许包含 Authorization 或其他密钥字段，避免密钥以明文进入数据库。

## 数据目录

- `data/pgagent.db`：工作区、Agent、会话、消息、运行、事件、审批、记忆、模型连接、Token 用量以及子 Agent 委派记录。
- `data/langgraph_checkpoints.db`：LangGraph checkpoint。
- `data/workspaces/default/`：一次性任务的默认工作区；也可以在会话页项目区或草稿输入框中通过原生目录窗口选择其他本地目录。
- `data/skills/{slug}/`：通过 API 导入并由 PGAgent 管理的 Skill 文件副本。原始本地目录不会被修改；GitHub/skills.sh 来源必须先预览文件清单，再以 `confirm=true` 明确复制。

工具执行前会解析真实路径并检查它仍位于所选工作区；Windows Junction 和符号链接不能用来逃逸工作区。`bash`/兼容的 `run_command` 仍以当前 Windows 用户权限运行，但只接受裸 allowlist 可执行文件且不会通过 shell 解析；即使在 `full` 模式也不会取消这些基础限制。

## Tool、Skill 与权限 API

- `GET /api/tools`：内置工具目录，字段包括 `id/name/label/description/category/risk_level/enabled/is_builtin/availability` 和可选的 `runtime_tool_id`。
- `GET /api/usage/runs/{run_id}`：读取单次运行的精确 Token/缓存/成本明细；无 provider 用量回报时返回 `null`。
- `GET /api/skills`、`POST /api/skills/import`：列出或安全导入本地 `SKILL.md` 文件夹。
- `GET /api/skills/market/status`、`POST /api/skills/market/search`：skills.sh 市场状态与搜索。市场 API 需要环境变量 `SKILLS_SH_API_TOKEN`（或 Vercel OIDC token）；未配置时会明确返回 `available=false`，不会伪造搜索结果。
- 本地启动会通过已登录且已关联项目的 Vercel CLI 刷新短期 OIDC token；运行中若 skills.sh 返回 401，后端会刷新一次并重试。刷新失败不会阻止 PGAgent 的其他本地功能启动。
- `POST /api/skills/market/install`：传入 `market_id` 或受限的公开 GitHub 仓库/ZIP `source_url`。默认只返回候选 Skill 与文件预览；再次携带 `confirm=true` 才会复制，不执行任何 Skill 脚本。
- `AgentCreate/AgentUpdate` 支持 `tool_ids`、`skill_ids`；`SessionCreate/SessionUpdate` 支持 `permission_mode`、`skill_ids`。相应 Read 响应始终返回这些字段。固定 PGAgent 主控展示全套内置工具，不能修改或删除。

## 开发与验证

```powershell
# 后端
conda activate E:\anaconda3\envs\agent_dock
cd C:\Users\xxr\Desktop\agent_test\PGAgent\backend
python -m pytest

# 前端
cd C:\Users\xxr\Desktop\agent_test\PGAgent\frontend
npm run dev
npm run build
npm run lint
npm test -- --run
```

后端开发服务：

```powershell
E:\anaconda3\envs\agent_dock\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765 --reload
```

## 第一版边界

- 单机单用户，不包含登录、云端部署和远程执行节点。
- 多 Agent 提供固定主控、DAG 依赖调度、单层并行子 Run、可复用队友身份、持久化消息总线和可选 Git worktree。单次 `task` 最多 8 个子任务；子 Agent 的工具和 Skill 配置已生效，且禁止递归委派和团队控制。
- 模型能力由提供商决定。部分中转站不支持工具调用或思考强度参数，PGAgent 会尽量丢弃不支持的可选参数，但无法替代提供商能力。
- 同步 SDK 调用超时后结果会被丢弃，但 Python 无法强杀已进入第三方库的线程；PGAgent 默认使用 LiteLLM 异步调用避免这一问题。

更多运行时设计细节见 `docs/ARCHITECTURE.md`，企鹅主题 UI 设计与交互约束见 `docs/UI_ARCHITECTURE.md`。
