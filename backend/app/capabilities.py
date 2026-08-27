"""Declarative capability catalog exposed by the PGAgent configuration API.

``availability='available'`` means the local ``ToolRegistry`` has a real
runtime implementation.  It still does not grant the capability by itself:
the Agent/session selection and the run permission mode decide whether that
tool appears in a particular model call. ``task`` receives a per-run,
database-backed child-Agent delegate and remains unavailable without an
enabled user-created target.
"""

from __future__ import annotations

from dataclasses import dataclass

from .tools.advanced_contract import CLAW_TOOL_NAMES, LEARN_TOOL_NAMES


@dataclass(frozen=True, slots=True)
class BuiltinTool:
    id: str
    name: str
    label: str
    description: str
    category: str
    risk_level: str
    enabled: bool
    availability: str
    runtime_tool_id: str | None = None
    requires_approval: bool = False


BUILTIN_TOOL_CATALOG: tuple[BuiltinTool, ...] = (
    BuiltinTool(
        id="bash",
        name="bash",
        label="运行命令",
        description="在当前项目目录中执行允许列表内的本地命令；智能审批会自动放行可识别的检查命令，危险或无法可靠判断的命令需确认。",
        category="execution",
        risk_level="adaptive",
        enabled=True,
        availability="available",
        runtime_tool_id="bash",
    ),
    BuiltinTool(
        id="read",
        name="read",
        label="读取文件",
        description="读取项目目录中的 UTF-8 文本文件。",
        category="filesystem",
        risk_level="low",
        enabled=True,
        availability="available",
        runtime_tool_id="read",
    ),
    BuiltinTool(
        id="read_artifact",
        name="read_artifact",
        label="读取上下文 Artifact",
        description="按字符分页读取当前会话拥有的工具输出或压缩对话 Artifact，不暴露服务器文件路径。",
        category="filesystem",
        risk_level="low",
        enabled=True,
        availability="available",
        runtime_tool_id="read_artifact",
    ),
    BuiltinTool(
        id="write",
        name="write",
        label="写入文件",
        description="创建或覆盖项目目录中的文件；智能审批会自动放行普通代码与文档修改，并对密钥、配置、脚本或大范围覆盖请求确认。",
        category="filesystem",
        risk_level="adaptive",
        enabled=True,
        availability="available",
        runtime_tool_id="write",
    ),
    BuiltinTool(
        id="edit",
        name="edit",
        label="编辑文件",
        description="精确替换项目文本文件中的内容；智能审批会自动放行普通代码修改，并对敏感或大范围变更请求确认。",
        category="filesystem",
        risk_level="adaptive",
        enabled=True,
        availability="available",
        runtime_tool_id="edit",
    ),
    BuiltinTool(
        id="delete",
        name="delete",
        label="删除文件",
        description="删除项目目录中的单个普通文件；不支持目录、递归删除或符号链接，并受工作区边界保护。",
        category="filesystem",
        risk_level="high",
        enabled=True,
        availability="available",
        runtime_tool_id="delete",
        requires_approval=True,
    ),
    BuiltinTool(
        id="glob",
        name="glob",
        label="列出文件",
        description="按 glob 模式查找项目目录内的文件和目录。",
        category="filesystem",
        risk_level="low",
        enabled=True,
        availability="available",
        runtime_tool_id="glob",
    ),
    BuiltinTool(
        id="grep",
        name="grep",
        label="搜索文件内容",
        description="在项目文本文件中使用受限正则搜索内容。",
        category="filesystem",
        risk_level="low",
        enabled=True,
        availability="available",
        runtime_tool_id="grep",
    ),
    BuiltinTool(
        id="webfetch",
        name="webfetch",
        label="抓取网页",
        description="抓取公开 HTTP/HTTPS 页面；拒绝内网、回环地址和不安全重定向。",
        category="network",
        risk_level="medium",
        enabled=True,
        availability="available",
        runtime_tool_id="webfetch",
    ),
    BuiltinTool(
        id="websearch",
        name="websearch",
        label="网页搜索",
        description="使用公开 DuckDuckGo HTML 搜索；网络不可用时明确报错而不编造结果。",
        category="network",
        risk_level="medium",
        enabled=True,
        availability="available",
        runtime_tool_id="websearch",
    ),
    BuiltinTool(
        id="task",
        name="task",
        label="委派任务",
        description="把一个或多个相互独立的子任务并行委派给已启用的用户创建子 Agent；会冻结模型、工具、Skill、工作区和权限边界，并在全部完成后返回结构化结果。",
        category="orchestration",
        risk_level="medium",
        enabled=True,
        availability="available",
        runtime_tool_id="task",
    ),
    BuiltinTool(
        id="todowrite",
        name="todowrite",
        label="任务清单",
        description="更新本次运行的结构化任务清单。",
        category="planning",
        risk_level="low",
        enabled=True,
        availability="available",
        runtime_tool_id="todowrite",
    ),
    BuiltinTool(
        id="question",
        name="question",
        label="向用户提问",
        description="向用户提出澄清问题并安全结束本轮，等待下一条用户消息。",
        category="interaction",
        risk_level="low",
        enabled=True,
        availability="available",
        runtime_tool_id="question",
    ),
    BuiltinTool(
        id="skill",
        name="skill",
        label="调用 Skill",
        description="按 ID 读取本会话已选 Skill 的文字指令；不会自动执行 Skill 脚本。",
        category="extension",
        risk_level="medium",
        enabled=True,
        availability="available",
        runtime_tool_id="skill",
    ),
    BuiltinTool(
        id="git_status",
        name="git_status",
        label="Git 状态",
        description="只读查看当前工作区的分支与变更状态，不会修改仓库。",
        category="developer",
        risk_level="low",
        enabled=True,
        availability="available",
        runtime_tool_id="git_status",
    ),
    BuiltinTool(
        id="git_diff",
        name="git_diff",
        label="Git 差异",
        description="只读查看受边界限制的 Git diff，可按工作区内路径过滤。",
        category="developer",
        risk_level="low",
        enabled=True,
        availability="available",
        runtime_tool_id="git_diff",
    ),
    BuiltinTool(
        id="file_info",
        name="file_info",
        label="文件信息",
        description="只读查看工作区内文件或目录的类型、大小和修改时间。",
        category="filesystem",
        risk_level="low",
        enabled=True,
        availability="available",
        runtime_tool_id="file_info",
    ),
)


_REFERENCE_TOOL_NAMES = ("read_file", "write_file", *CLAW_TOOL_NAMES, *LEARN_TOOL_NAMES)
_REFERENCE_NETWORK = {"WebFetch", "WebSearch", "RemoteTrigger", "MCP", "McpAuth"}
_REFERENCE_EXECUTION = {"PowerShell", "REPL", "background_run"}
_REFERENCE_FILESYSTEM = {
    "read_file", "write_file", "edit_file", "glob_search", "grep_search",
    "NotebookEdit", "MemoryWrite", "MemoryRead", "MemoryList", "MemorySearch",
}
_REFERENCE_HIGH_RISK = {
    "write_file", "edit_file", "NotebookEdit", "PowerShell", "REPL", "RemoteTrigger",
    "MCP", "MemoryWrite", "background_run", "integrate_teammate", "CronCreate", "CronDelete",
}

# The reference projects expose a broad built-in contract.  These entries are
# real runtime capabilities, not UI-only placeholders, and are enabled for new
# default agents exactly like PGAgent's compact aliases above.
BUILTIN_TOOL_CATALOG = (
    *BUILTIN_TOOL_CATALOG,
    *(
        BuiltinTool(
            id=name,
            name=name,
            label=name,
            description=f"Learn Claude Code / Claw Code compatible {name} capability.",
            category=(
                "network" if name in _REFERENCE_NETWORK
                else "execution" if name in _REFERENCE_EXECUTION
                else "filesystem" if name in _REFERENCE_FILESYSTEM
                else "orchestration"
            ),
            risk_level="adaptive" if name in _REFERENCE_HIGH_RISK else "low",
            enabled=True,
            availability="available",
            runtime_tool_id=name,
            requires_approval=name in _REFERENCE_HIGH_RISK,
        )
        for name in _REFERENCE_TOOL_NAMES
    ),
)

BUILTIN_TOOL_BY_ID = {item.id: item for item in BUILTIN_TOOL_CATALOG}
BUILTIN_TOOL_IDS = tuple(item.id for item in BUILTIN_TOOL_CATALOG)
