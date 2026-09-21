"""Declarative Codex-style capability catalog exposed by the configuration API.

Only canonical tools intended for new Agents belong here. Historic Claude,
Claw, and pre-Codex PGAgent names remain executable in ``ToolRegistry`` so an
already persisted run can resume, but they are absent from this catalog.
"""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 catalog 子模块。
# 逻辑关系：上层通过 tools/catalog.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import dataclass


# 类职责：定义 BuiltinTool 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class BuiltinTool:
    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：label 表示当前步骤使用的 label 值。
    label: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：category 表示当前步骤使用的 category 值。
    category: str
    # 变量说明：risk_level 表示当前步骤使用的 risk_level 值。
    risk_level: str
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool = True
    # 变量说明：availability 表示当前步骤使用的 availability 值。
    availability: str = "available"
    # 变量说明：runtime_tool_id 表示runtime_tool 对象的唯一标识。
    runtime_tool_id: str | None = None
    # 变量说明：requires_approval 表示当前步骤使用的 requires_approval 值。
    requires_approval: bool = False


# 函数职责：完成 tool 对应的业务处理。
# 参数关系：name 表示当前对象名称；label 表示当前步骤使用的 label 值；description 表示当前步骤使用的 description 值；category 表示当前步骤使用的 category 值；risk_level 表示当前步骤使用的 risk_level 值；requires_approval 表示当前步骤使用的 requires_approval 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _tool(
    name: str,
    label: str,
    description: str,
    category: str,
    risk_level: str = "low",
    *,
    requires_approval: bool = False,
) -> BuiltinTool:
    return BuiltinTool(
        id=name,
        name=name,
        label=label,
        description=description,
        category=category,
        risk_level=risk_level,
        runtime_tool_id=name,
        requires_approval=requires_approval,
    )


# 变量说明：BUILTIN_TOOL_CATALOG 表示当前步骤使用的 BUILTIN_TOOL_CATALOG 值。
BUILTIN_TOOL_CATALOG: tuple[BuiltinTool, ...] = (
    _tool("shell", "运行命令", "在 Windows 项目目录中通过 PowerShell 执行命令；支持管道和 cmdlet。长命令会返回可继续读取的 session_id；后台失败时先读取输出再决定是否重试。", "execution", "adaptive"),
    _tool("write_stdin", "继续命令", "向持久后台命令写入输入或读取后续输出；使用 task_id，取值为 shell/background_run 返回的 session_id。后台命令失败时先检查已有输出，不要直接重复执行原命令。", "execution", "adaptive"),
    _tool("read", "读取文件", "分页读取项目目录中的 UTF-8 文本文件。", "filesystem"),
    _tool("read_artifact", "读取 Artifact", "分页读取当前会话拥有的大型工具输出或压缩对话。", "filesystem"),
    _tool("glob", "列出文件", "按 glob 模式查找项目目录内的文件和目录；pattern 与 path 必须是工作区相对路径，未指定 path 时使用 '.'，不要传入盘符或绝对路径。", "filesystem"),
    _tool("rg", "搜索代码", "使用 ripgrep 按模式、相对路径和文件 glob 快速检索代码；若返回 tool_unavailable，先通过 tool_search 激活 grep，再使用 grep 完成搜索。", "developer"),
    _tool(
        "apply_patch",
        "应用补丁",
        "应用严格的多文件文本补丁；路径必须是工作区相对路径，Add File 区段每一行都必须以 + 开头，Update File 必须使用 @@ hunk。",
        "developer",
        "adaptive",
    ),
    _tool(
        "git_status",
        "Git 状态",
        "只读查看当前工作区的分支与变更状态；调用前会检查是否位于 Git 仓库。非 Git 目录应跳过 Git 检查。",
        "developer",
    ),
    _tool(
        "git_diff",
        "Git 差异",
        "只读查看受边界限制的 Git diff；仅在当前工作区是 Git 仓库时调用。",
        "developer",
    ),
    _tool("validate", "验证变更", "运行测试、lint、类型检查或构建并记录结果。", "developer", "adaptive"),
    _tool("validate_baseline", "验证基线", "在隔离 worktree 中对 pristine HEAD 运行检查。", "developer", "adaptive"),
    _tool("review_finding", "记录审查发现", "记录带位置、证据和失败场景的代码审查发现。", "developer"),
    _tool("debug_evidence", "记录调试证据", "记录复现、观察、假设、根因或回归结果。", "developer"),
    _tool("web_search", "联网搜索", "使用模型连接提供的原生联网搜索能力并保留来源。", "network", "medium"),
    _tool("web_open", "打开网页", "提取公开网页的标题、发布时间和有限正文；支持分页续读。", "network", "medium"),
    _tool("web_run", "联网工具", "Codex 风格联网入口，支持搜索、打开、查找、截图、财经、天气、体育和时间查询。open 只能使用真实 http(s) URL 或先前结果返回的 ref_id；unsafe_url、无效参数和无效 ref_id 不要原样重试，站点 401/403/404/429 时改用其他来源。", "network", "medium"),
    _tool("update_plan", "更新计划", "创建或更新本次运行的结构化任务计划。", "planning"),
    _tool("task", "委派任务", "把独立任务委派给已启用的用户创建子 Agent。", "orchestration", "medium"),
    _tool("tool_search", "搜索工具", "按需搜索并激活当前 Agent 已选择的低频工具。", "extension"),
    _tool("question", "向用户提问", "信息不足时提出澄清问题并等待用户回复。", "interaction"),
)

# 变量说明：BUILTIN_TOOL_BY_ID 表示BUILTIN_TOOL_BY 对象的唯一标识。
BUILTIN_TOOL_BY_ID = {item.id: item for item in BUILTIN_TOOL_CATALOG}
# 变量说明：BUILTIN_TOOL_IDS 表示BUILTIN_TOOL 对象标识集合。
BUILTIN_TOOL_IDS = tuple(item.id for item in BUILTIN_TOOL_CATALOG)
