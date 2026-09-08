# 文件职责：保存单次采样时的工具快照，避免采样后工具激活状态变化影响本轮解释。
"""Immutable tool view captured for one model sampling request."""

from __future__ import annotations

from dataclasses import dataclass

from src.tools.plan import ToolPlan


# 类职责：封装 AgentStepContext 的状态、依赖和领域行为。
# 协作关系：实例由运行服务或相邻节点创建，并在智能体步骤之间传递数据。
@dataclass(frozen=True, slots=True)
class AgentStepContext:
    # step_number 标识采样轮次；tool_plan 关联当轮模型可见描述和可执行注册表。
    # 变量说明：step_number 表示当前步骤使用的 step_number 值。
    step_number: int
    # 变量说明：tool_plan 表示当前步骤使用的 tool_plan 值。
    tool_plan: ToolPlan
