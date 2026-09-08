"""Request-scoped tool plan advertised to and executed for one model step."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 plan 子模块。
# 逻辑关系：上层通过 tools/plan.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .name import ToolName

if TYPE_CHECKING:
    from .registry import ToolRegistry


# 类职责：定义 ToolPlan 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class ToolPlan:
    # 变量说明：registry 表示当前步骤使用的 registry 值。
    registry: "ToolRegistry"
    # 变量说明：model_specs 表示当前流程使用的 model_specs 集合。
    model_specs: tuple[dict, ...]
    # 变量说明：wire_names 表示当前流程使用的 wire_names 集合。
    wire_names: dict[str, ToolName]
    # 变量说明：discoverable_tools 表示当前流程使用的 discoverable_tools 集合。
    discoverable_tools: tuple[ToolName, ...]
    # 变量说明：activated_tools 表示当前流程使用的 activated_tools 集合。
    activated_tools: tuple[ToolName, ...]


# 类职责：定义 ToolPlanBuilder 在本领域中的数据与行为。
class ToolPlanBuilder:
    # 函数职责：完成 build 对应的业务处理。
    # 参数关系：registry 表示当前步骤使用的 registry 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def build(self, registry: "ToolRegistry") -> ToolPlan:
        # 变量说明：runtimes 表示当前流程使用的 runtimes 集合。
        runtimes = tuple(registry.runtimes)
        # 变量说明：active 表示当前步骤使用的 active 值。
        active = set(registry.activated_tool_names)
        return ToolPlan(
            registry=registry,
            model_specs=tuple(registry.schemas),
            wire_names={runtime.identity.wire_name: runtime.identity.canonical_name for runtime in runtimes},
            discoverable_tools=tuple(
                runtime.identity.canonical_name
                for runtime in runtimes
                if runtime.presentation.discoverable
            ),
            activated_tools=tuple(
                runtime.identity.canonical_name
                for runtime in runtimes
                if runtime.identity.wire_name in active
            ),
        )
