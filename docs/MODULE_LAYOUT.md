# PGAgent 模块目录与所有权

PGAgent 后端按 Codex `codex-rs/core` 的层级理解：`backend/` 对应一个完整的后端核心 crate，`backend/src/` 才是实现源码。源码目录优先表达业务领域，文件名再表达该领域内的职责。

## 后端

```text
backend/                              # 完整后端核心，对应 codex-rs/core/
├── README.md                         # 后端边界和目录索引
├── requirements.txt                 # Python 运行依赖
├── pytest.ini                        # 后端测试配置
├── src/                              # 实现源码，对应 core/src/
│   ├── main.py                       # FastAPI 组装与进程生命周期入口
│   ├── agent/                        # 单个 Agent 的执行内核
│   │   ├── engine.py                 # 模型/工具循环
│   │   ├── loop.py                   # 显式 Agent 生命周期循环
│   │   ├── state.py                  # 单次运行状态结构
│   │   ├── completion.py             # 确定性完成判定
│   │   ├── guards.py                 # 循环和无进展检测
│   │   └── errors.py                 # 模型调用错误分类与重试
│   ├── agents/                       # 多 Agent 协作
│   │   └── collaboration.py          # 队友、邮箱和协作事件
│   ├── api/                          # HTTP 边界
│   │   ├── routes/                   # 按公开资源拆分的路由
│   │   ├── schemas.py                # API 请求/响应契约
│   │   ├── runtime.py                # 运行控制与事件流入口
│   │   ├── capabilities.py           # 能力目录 API
│   │   ├── connections.py            # 模型连接 API
│   │   ├── system.py                 # 本机系统 API
│   │   └── usage.py                  # 用量 API
│   ├── artifacts/                    # 大型工具输出和产物存储
│   ├── config/                       # 配置、环境变量和路径
│   ├── context/                      # 上下文装配、窗口、压缩和指令
│   ├── memory/                       # 长期记忆导入、检索和投影
│   ├── mcp/                          # MCP Client 配置、会话连接与工具适配
│   ├── model/                        # 模型网关与凭据适配
│   ├── native/                       # Windows 原生系统集成
│   ├── persistence/                  # ORM、数据库生命周期和默认数据
│   ├── runs/                         # Run 配置、生命周期、委派和事件流
│   ├── sessions/                     # 会话交付语义
│   ├── skills/                       # Skill 注册、导入和选择
│   ├── tasks/                        # 持久任务图、状态与后台作业
│   └── tools/                        # 工具目录、注册、策略和沙箱
└── tests/                            # 与 src 领域对应的后端测试
```

### 边界规则

- `api/` 只处理 HTTP 输入输出、资源查找和事务提交，不承载 Agent 执行流程。
- `agent/` 只负责单次模型/工具执行；跨 Run 的恢复、委派和交付归 `runs/`。
- `tasks/` 保存可恢复的任务状态；`sessions/` 负责用户可见消息的唯一交付。
- `context/` 负责送入模型的内容；`memory/` 负责跨会话的长期信息。
- `persistence/models.py` 只声明关系实体；引擎、迁移、初始化和数据库会话归 `persistence/database.py`。
- `main.py` 只装配各模块与进程生命周期，不新增业务实现。
- 不再保留 `app/`、`services/`、`runtime/` 等旧实现路径；新增代码必须进入拥有该行为的领域目录。

## 前端

```text
frontend/src/
├── app/AppShell.tsx                   # 导航、主题、健康检查和路由装配
├── features/                          # 按页面/业务能力组织
│   ├── dashboard/
│   ├── agents/
│   ├── sessions/
│   │   ├── SessionsPage.tsx           # 会话页面组合层
│   │   ├── sessionState.ts            # 会话局部类型和常量
│   │   ├── hooks/                     # SSE、轮询与运行状态同步
│   │   └── components/                # 输入框、任务和消息组件
│   ├── runs/
│   ├── skills/
│   ├── memories/
│   ├── models/
│   ├── usage/
│   └── settings/
├── shared/                            # 稳定的跨功能 UI、Hook 和纯工具
├── styles/                            # token、壳层和业务域样式
└── App.tsx                            # 极薄应用入口
```

页面负责组合状态和组件；独立网络生命周期放入 feature hook，纯展示块放入 feature component。只有稳定的跨功能复用才进入 `shared/`。
