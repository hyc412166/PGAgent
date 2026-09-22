# PGAgent

PGAgent 是一个本地优先、单用户的 Agent 工作台。它把项目、会话、模型、工具、权限审批和运行记录放在同一个可追踪的工作流中，适合在本机目录中完成编码、资料整理、文件处理和自动化任务。

项目强调可恢复和可观察性：每次用户输入都有独立的会话轮次和运行记录，工具调用、审批、后台任务、模型重试和终态回复都可以追踪。模型只会看到当前运行快照中明确启用的工具，文件与命令操作仍受工作区边界和命令 allowlist 约束。

## 功能概览

- 项目、会话、运行记录、用量统计、模型设置和 Agent 管理界面。
- 会话轮次导航：在长会话中快速定位轮次，并悬浮预览提问与回答摘要。
- OpenAI 兼容接口、OpenRouter、DeepSeek 等模型连接；支持 Base URL、手动模型 ID 和思考强度。
- 固定 PGAgent 主控，可按需规划任务、调用工具、汇总结果并委派独立上下文的子 Agent。
- 带依赖关系的任务图：无依赖步骤可以并发执行，后继步骤在前置步骤成功后解锁。
- 文件读写、编辑、搜索、命令、联网和 Git 查看等工具，并对工具目录进行运行时冻结。
- `ask`、`smart`、`full` 三种权限模式，写入、命令、联网和委派操作可进入人工审批。
- 后台任务、运行恢复、确定性终态回复和带追踪号的异常说明。
- Skill 导入与 MCP Client；Skill 只读取指令文本，不会在导入或预览阶段执行脚本。
- 浅色/深色主题、响应式布局、Token 用量和成本统计。

## 技术结构

```text
backend/   FastAPI 服务、Agent Runtime、模型适配器、SQLite 持久化和运行事件
frontend/  React + Vite 工作台
api/       Vercel Skill 市场网关
scripts/   本地开发与启动辅助脚本
docs/      架构、模块布局和 UI 设计说明
```

核心设计说明见：

- [架构说明](docs/ARCHITECTURE.md)
- [模块布局](docs/MODULE_LAYOUT.md)
- [UI 架构](docs/UI_ARCHITECTURE.md)

## 环境要求

- Python 3.12 或更高版本
- Node.js 22 或兼容的现代 Node.js 版本
- npm
- Windows、macOS 或 Linux 均可运行后端与前端；仓库中的 `.bat` 文件仅适用于 Windows

模型服务需要由使用者自行提供。PGAgent 不内置模型，也不会在仓库中保存 API Key。

## 安装

在仓库根目录执行：

```powershell
# 后端依赖
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt

# 前端依赖
npm --prefix frontend install
```

Linux/macOS 用户可以使用等价的虚拟环境激活命令：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
npm --prefix frontend install
```

仓库中的 `setup_pgagent.bat` 和 `start_pgagent.bat` 是 Windows 辅助入口。它们依赖本机已有的 PowerShell、Node.js 和 Python 安装；如果本机路径不同，请使用手动命令，或先调整脚本中的环境路径。

## 配置

复制环境变量模板：

```powershell
Copy-Item .env.example .env.local
```

至少需要配置一个模型连接。常用运行参数包括：

```dotenv
PGAGENT_HOST=127.0.0.1
PGAGENT_PORT=8765
PGAGENT_LOG_LEVEL=INFO
PGAGENT_LOG_DIR=data/logs
PGAGENT_CONTEXT_LIMIT_TOKENS=200000
PGAGENT_COMPACT_THRESHOLD_TOKENS=180000
```

模型连接、API Key 和自定义 Header 应通过本地环境或工作台设置提供。真实密钥不得写入 README、提交记录、`.env.example` 或其他公开文件。

可选配置：

- `PGAGENT_SKILL_MARKET_URL` 与 `PGAGENT_SKILL_MARKET_CLIENT_TOKEN`：Skill 市场网关。
- `PGAGENT_MCP_CONFIG_PATH`：自定义 MCP JSON 配置文件路径。
- `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`：联网工具使用的代理设置。
- `PGAGENT_BRAVE_SEARCH_API_KEY`：可选的搜索服务凭据。

## 启动开发环境

先启动后端：

```powershell
python -m uvicorn src.main:app --app-dir backend --host 127.0.0.1 --port 8765 --reload
```

再启动前端开发服务器：

```powershell
npm --prefix frontend run dev
```

生产构建：

```powershell
npm --prefix frontend run build
```

默认后端地址为 `http://127.0.0.1:8765`。端口可以通过环境变量调整；请勿把本地服务地址、调试端口或运行日志当作公开 API 配置提交。

## 第一次使用

1. 打开模型设置，新增模型连接并填写 Base URL、模型 ID 和本地凭据。
2. 打开会话页面，在项目区域添加一个本地工作区目录。
3. 创建新对话并发送任务；首次发送后才会创建持久化会话和运行记录。
4. 根据权限模式审批文件写入、命令、联网或 Agent 委派操作。
5. 在运行记录中查看步骤、耗时、Token、成本、工具结果和终态状态。

API Key 会保存到操作系统凭据存储，不写入 SQLite。数据库只保存引用标识；自定义 Header 也不会允许直接保存授权密钥。

## 工具、Skill 与 MCP

PGAgent 内置工具包括文件读取、写入、编辑、搜索、受限命令、联网、任务委派、待办、提问和 Git 查看等能力。具体工具是否可用由 Agent、会话和运行快照共同决定。

Skill 导入只复制并解析 `SKILL.md` 元数据和指令文本，不会自动执行 Skill 中的脚本。MCP Client 支持本地 stdio server 和远端 Streamable HTTP；凭据应通过环境变量引用：

```json
{
  "mcpServers": {
    "docs": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-everything"],
      "required": false,
      "enabled_tools": ["echo"]
    },
    "remote": {
      "url": "https://example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${EXAMPLE_MCP_TOKEN}"
      },
      "enabled": false
    }
  }
}
```

详细配置示例见 [`data/mcp.example.json`](data/mcp.example.json)。实际的 `data/mcp.json` 只应保存在本机。

## 开发与验证

```powershell
# 后端测试
Push-Location backend
python -m pytest
Pop-Location

# 前端构建、Lint 和测试
npm --prefix frontend run build
npm --prefix frontend run lint
npm --prefix frontend test -- --run

# Skill 市场网关测试
npm run test:gateway
```

后端日志使用 JSONL 写入 `data/logs/`，用于诊断运行边界、重试、异常和终态交付。日志可能包含本机运行上下文，不应提交到远程仓库。

## Git 与敏感文件

以下内容属于本机状态或敏感信息，不应提交：

- `.env.local`、API Key、OAuth/OIDC Token 和自定义凭据
- `.vercel/`、`.omo/`、`.agents/`、`.codex/` 等本地工具状态目录
- `data/*.db`、`data/logs/`、`data/mcp.json` 和本地工作区产物
- 前端依赖、构建产物和缓存目录

仓库中的 `.gitignore`、`.vercelignore`、`vercel.json` 和示例配置可以提交；它们只描述项目协作或部署规则，不包含本机运行状态。

## 当前边界

- 当前定位是单机、单用户工作台，不包含登录系统和远程执行节点。
- 模型能力取决于所连接的 provider；部分 provider 不支持工具调用或思考强度参数。
- 受限命令仍以当前操作系统用户权限运行，但会受到 allowlist、工作区路径和符号链接边界检查。
- 同步第三方 SDK 调用超时后无法强杀底层线程；默认运行路径使用异步调用。

## 许可证

如果仓库尚未声明正式许可证，请在公开发布前补充 LICENSE 文件并明确第三方依赖的许可证要求。
