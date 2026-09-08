"""Shared workflow-profile contract layered on the common AgentRuntime."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 base 子模块。
# 逻辑关系：上层通过 coding/profiles/base.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import dataclass


# 类职责：定义 WorkflowProfile 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class WorkflowProfile:
    """Prompt and tool-presentation policy for one engineering workflow."""

    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：direct_tool_names 表示当前流程使用的 direct_tool_names 集合。
    direct_tool_names: frozenset[str]
    # 变量说明：instructions 表示当前流程使用的 instructions 集合。
    instructions: str
    # 变量说明：read_only_tool_ceiling 表示当前步骤使用的 read_only_tool_ceiling 值。
    read_only_tool_ceiling: bool = False
    # 变量说明：allowed_non_read_only_tool_names 表示当前流程使用的 allowed_non_read_only_tool_names 集合。
    allowed_non_read_only_tool_names: frozenset[str] = frozenset()
