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
        description="在当前项目目录中执行允许列表内的本地命令；执行前需要用户审批。",
        category="execution",
        risk_level="high",
        enabled=True,
        availability="available",
        runtime_tool_id="bash",
        requires_approval=True,
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
        id="write",
        name="write",
        label="写入文件",
        description="创建或覆盖项目目录中的文件；写入前需要用户审批。",
        category="filesystem",
        risk_level="high",
        enabled=True,
        availability="available",
        runtime_tool_id="write",
        requires_approval=True,
    ),
    BuiltinTool(
        id="edit",
        name="edit",
        label="编辑文件",
        description="精确替换项目文本文件中的内容；修改前依权限模式决定是否审批。",
        category="filesystem",
        risk_level="high",
        enabled=True,
        availability="available",
        runtime_tool_id="edit",
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
        description="把明确子任务委派给已启用的用户创建子 Agent；会冻结模型、工具、Skill、工作区和权限边界，并返回结构化结果。",
        category="orchestration",
        risk_level="medium",
        enabled=True,
        availability="available",
        runtime_tool_id="task",
        requires_approval=True,
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
)

BUILTIN_TOOL_BY_ID = {item.id: item for item in BUILTIN_TOOL_CATALOG}
BUILTIN_TOOL_IDS = tuple(item.id for item in BUILTIN_TOOL_CATALOG)
