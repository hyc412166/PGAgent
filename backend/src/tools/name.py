"""Structured tool identity shared by planning, routing and persistence."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 name 子模块。
# 逻辑关系：上层通过 tools/name.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import dataclass


# 类职责：定义 ToolName 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True, order=True)
class ToolName:
    """Canonical name independent from the provider-facing wire name."""

    # 变量说明：namespace 表示当前步骤使用的 namespace 值。
    namespace: str
    # 变量说明：name 表示当前对象名称。
    name: str

    # 函数职责：完成 post_init 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __post_init__(self) -> None:
        # 变量说明：namespace 表示当前步骤使用的 namespace 值。
        namespace = self.namespace.strip()
        # 变量说明：name 表示当前对象名称。
        name = self.name.strip()
        if not namespace or not name:
            raise ValueError("tool namespace and name must not be empty")
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "name", name)

    # 函数职责：完成 builtin 对应的业务处理。
    # 参数关系：name 表示当前对象名称。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def builtin(cls, name: str) -> "ToolName":
        return cls("builtin", name)

    # 函数职责：完成 external 对应的业务处理。
    # 参数关系：owner 表示当前步骤使用的 owner 值；name 表示当前对象名称。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def external(cls, owner: str, name: str) -> "ToolName":
        return cls(owner, name)

    # 函数职责：完成 canonical 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def canonical(self) -> str:
        return f"{self.namespace}.{self.name}"

    # 函数职责：完成 str 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __str__(self) -> str:
        return self.canonical
