"""Compatibility imports for the original coding-profile module."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 profile 子模块。
# 逻辑关系：上层通过 coding/profile.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from typing import Iterable

from .profiles import CODING_PROFILE, WorkflowProfile, resolve_workflow_profile


# 变量说明：CodingProfile 表示当前步骤使用的 CodingProfile 值。
CodingProfile = WorkflowProfile


# 函数职责：解析 coding_profile 对应的数据或流程。
# 参数关系：enabled_tool_names 表示当前流程使用的 enabled_tool_names 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def resolve_coding_profile(enabled_tool_names: Iterable[str]) -> CodingProfile | None:
    """Select the legacy auto-detected coding workflow."""

    return resolve_workflow_profile("auto", enabled_tool_names)
