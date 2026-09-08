"""Canonical invocation and dispatch outcomes for every tool source."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 invocation 子模块。
# 逻辑关系：上层通过 tools/invocation.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .name import ToolName
from .types import ApprovalRequest, ToolResult


# 类职责：定义 ApprovalGrant 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """A grant bound to the exact persisted invocation, without a derived hash."""

    # 变量说明：approval_id 表示approval 对象的唯一标识。
    approval_id: str
    # 变量说明：call_id 表示call 对象的唯一标识。
    call_id: str
    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    tool_name: ToolName
    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments: dict[str, Any]

    # 函数职责：完成 matches 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def matches(self, invocation: "ToolInvocation") -> bool:
        return (
            self.call_id == invocation.call_id
            and self.tool_name == invocation.tool_name
            and self.arguments == invocation.arguments
        )


# 类职责：定义 ToolInvocation 在本领域中的数据与行为。
@dataclass(slots=True)
class ToolInvocation:
    # 变量说明：call_id 表示call 对象的唯一标识。
    call_id: str
    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    tool_name: ToolName
    # 变量说明：wire_name 表示当前步骤使用的 wire_name 值。
    wire_name: str
    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments: dict[str, Any]
    # 变量说明：source 表示当前步骤使用的 source 值。
    source: str = "model"
    # 变量说明：run_id 表示当前运行标识。
    run_id: str | None = None
    # 变量说明：session_id 表示所属会话标识。
    session_id: str | None = None
    # 变量说明：step_number 表示当前步骤使用的 step_number 值。
    step_number: int | None = None
    # 变量说明：approval 表示当前步骤使用的 approval 值。
    approval: ApprovalGrant | None = None
    # 变量说明：parent_invocation_id 表示parent_invocation 对象的唯一标识。
    parent_invocation_id: str | None = None
    # 变量说明：context 表示当前步骤使用的 context 值。
    context: dict[str, Any] = field(default_factory=dict)


# 类职责：定义 ToolDispatchOutcome 在本领域中的数据与行为。
class ToolDispatchOutcome:
    """Marker base for pipeline outcomes consumed by AgentRuntime."""


# 类职责：定义 Executed 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
@dataclass(slots=True)
class Executed(ToolDispatchOutcome):
    # 变量说明：result 表示本步骤产生的结果。
    result: ToolResult


# 类职责：定义 AwaitingApproval 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
@dataclass(slots=True)
class AwaitingApproval(ToolDispatchOutcome):
    # 变量说明：request 表示调用方传入的请求数据。
    request: ApprovalRequest
    # 变量说明：invocation 表示当前步骤使用的 invocation 值。
    invocation: ToolInvocation


# 类职责：定义 Rejected 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
@dataclass(slots=True)
class Rejected(ToolDispatchOutcome):
    # 变量说明：result 表示本步骤产生的结果。
    result: ToolResult


# 类职责：定义 Cancelled 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
@dataclass(slots=True)
class Cancelled(ToolDispatchOutcome):
    # 变量说明：result 表示本步骤产生的结果。
    result: ToolResult


# 类职责：定义 DispatchFailed 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
@dataclass(slots=True)
class DispatchFailed(ToolDispatchOutcome):
    # 变量说明：result 表示本步骤产生的结果。
    result: ToolResult
