"""Shared value objects used by tools and the agent runtime."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 types 子模块。
# 逻辑关系：上层通过 tools/types.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


# 类职责：定义 ApprovalRequest 的跨层数据契约。
@dataclass(slots=True)
class ApprovalRequest:
    """A side-effecting tool call waiting for an explicit user decision."""

    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    tool_name: str
    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments: dict[str, Any]
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str
    # 变量说明：id 表示当前对象的唯一标识。
    id: str = field(default_factory=lambda: str(uuid4()))
    # 变量说明：created_at 表示创建时间。
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # 函数职责：完成 to_dict 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 类职责：定义 ToolResult 在本领域中的数据与行为。
@dataclass(slots=True)
class ToolResult:
    """Normalized result returned by every built-in tool."""

    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    tool_name: str
    # 变量说明：ok 表示当前步骤使用的 ok 值。
    ok: bool
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str = ""
    # 变量说明：changed 表示当前步骤使用的 changed 值。
    changed: bool = False
    # 变量说明：approval_required 表示当前步骤使用的 approval_required 值。
    approval_required: bool = False
    # 变量说明：approval_request 表示当前步骤使用的 approval_request 值。
    approval_request: ApprovalRequest | None = None
    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
    error_code: str | None = None
    # 变量说明：metadata 表示当前步骤使用的 metadata 值。
    metadata: dict[str, Any] = field(default_factory=dict)

    # 函数职责：完成 made_progress 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def made_progress(self) -> bool:
        return self.ok and (self.changed or bool(self.content.strip()))

    # 函数职责：完成 to_dict 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def to_dict(self) -> dict[str, Any]:
        # 变量说明：value 表示当前字段或计算值。
        value = asdict(self)
        return value
