# PGAgent backend

`backend/` 是 PGAgent 的完整后端核心，目录层级对应 Codex 仓库中的 `codex-rs/core/`；`backend/src/` 对应 `core/src/`，用于放置实际实现。

## 目录索引

| 路径 | 负责内容 |
| --- | --- |
| `src/agent/` | 单个 Agent 的模型调用、工具循环、完成判定和运行保护 |
| `src/agents/` | 多 Agent 队友、邮箱与协作 |
| `src/api/` | FastAPI 路由、HTTP 契约和协议转换 |
| `src/artifacts/` | 大型工具输出与文件产物 |
| `src/config/` | 应用设置、环境变量与项目路径 |
| `src/context/` | 上下文窗口、装配、压缩与指令 |
| `src/memory/` | 跨会话记忆的存储、检索与 Markdown 投影 |
| `src/model/` | 模型提供商网关与密钥存储适配 |
| `src/native/` | Windows 原生对话框等本机操作系统集成 |
| `src/persistence/` | SQLAlchemy 实体、数据库引擎、迁移和默认数据 |
| `src/runs/` | Run 生命周期、恢复、配置、委派和事件流 |
| `src/sessions/` | 用户轮次与终态消息交付 |
| `src/skills/` | Skill 导入、注册和运行时选择 |
| `src/tasks/` | 持久任务图、任务状态和后台作业 |
| `src/tools/` | 工具实现、目录、注册、策略与工作区沙箱 |
| `tests/` | 后端行为与回归测试 |

## 入口

```powershell
E:\anaconda3\envs\agent_dock\python.exe -m uvicorn src.main:app --app-dir backend --host 127.0.0.1 --port 8765
```

`src/main.py` 只负责装配和进程生命周期。新增功能应进入拥有该行为的领域目录，不得重新创建通用 `services/` 或 `runtime/` 聚合目录。
