"""Declarative Codex-style capability catalog exposed by the configuration API.

Only canonical tools intended for new Agents belong here. Historic Claude,
Claw, and pre-Codex PGAgent names remain executable in ``ToolRegistry`` so an
already persisted run can resume, but they are absent from this catalog.
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
    enabled: bool = True
    availability: str = "available"
    runtime_tool_id: str | None = None
    requires_approval: bool = False


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


BUILTIN_TOOL_CATALOG: tuple[BuiltinTool, ...] = (
    _tool("shell", "运行命令", "在当前项目目录执行受控命令；长任务可返回持久 session_id。", "execution", "adaptive"),
    _tool("write_stdin", "继续命令", "向持久命令会话写入输入或读取后续输出。", "execution", "adaptive"),
    _tool("read", "读取文件", "分页读取项目目录中的 UTF-8 文本文件。", "filesystem"),
    _tool("read_artifact", "读取 Artifact", "分页读取当前会话拥有的大型工具输出或压缩对话。", "filesystem"),
    _tool("glob", "列出文件", "按 glob 模式查找项目目录内的文件和目录。", "filesystem"),
    _tool("rg", "搜索代码", "使用 ripgrep 按模式、路径和文件 glob 快速检索代码。", "developer"),
    _tool("apply_patch", "应用补丁", "应用严格的多文件文本补丁。", "developer", "adaptive"),
    _tool("git_status", "Git 状态", "只读查看当前工作区的分支与变更状态。", "developer"),
    _tool("git_diff", "Git 差异", "只读查看受边界限制的 Git diff。", "developer"),
    _tool("validate", "验证变更", "运行测试、lint、类型检查或构建并记录结果。", "developer", "adaptive"),
    _tool("validate_baseline", "验证基线", "在隔离 worktree 中对 pristine HEAD 运行检查。", "developer", "adaptive"),
    _tool("review_finding", "记录审查发现", "记录带位置、证据和失败场景的代码审查发现。", "developer"),
    _tool("debug_evidence", "记录调试证据", "记录复现、观察、假设、根因或回归结果。", "developer"),
    _tool("web_search", "联网搜索", "使用模型连接提供的原生联网搜索能力并保留来源。", "network", "medium"),
    _tool("web_open", "打开网页", "提取公开网页的标题、发布时间和有限正文；支持分页续读。", "network", "medium"),
    _tool("update_plan", "更新计划", "创建或更新本次运行的结构化任务计划。", "planning"),
    _tool("task", "委派任务", "把独立任务委派给已启用的用户创建子 Agent。", "orchestration", "medium"),
    _tool("tool_search", "搜索工具", "按需搜索并激活当前 Agent 已选择的低频工具。", "extension"),
    _tool("skill", "调用 Skill", "读取本会话已选 Skill 的文字指令。", "extension", "medium"),
    _tool("question", "向用户提问", "信息不足时提出澄清问题并等待用户回复。", "interaction"),
)

BUILTIN_TOOL_BY_ID = {item.id: item for item in BUILTIN_TOOL_CATALOG}
BUILTIN_TOOL_IDS = tuple(item.id for item in BUILTIN_TOOL_CATALOG)
