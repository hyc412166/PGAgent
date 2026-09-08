"""为运行、工具和后台任务提供可传播的诊断关联上下文。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Iterator, TypeAlias


JSONScalar: TypeAlias = str | int | float | bool | None

# 这些字段会被日志、事件和后台续跑共同使用；未知字段不进入公共诊断上下文。
CONTEXT_KEYS = frozenset(
    {
        "trace_id",
        "run_id",
        "turn_id",
        "parent_run_id",
        "step",
        "tool_call_id",
        "tool_name",
        "event_type",
        "background_job_id",
        "child_run_id",
    }
)


# ContextVar 默认值和每次绑定值都不可变，避免一个请求修改另一个 asyncio task 的状态。
_CONTEXT: ContextVar[MappingProxyType[str, JSONScalar]] = ContextVar(
    "pgagent_observability_context",
    default=MappingProxyType({}),
)


@contextmanager
def bind_observability_context(**fields: JSONScalar) -> Iterator[None]:
    """在当前执行上下文中合并关联字段，并在离开时恢复之前的值。"""

    clean = {
        key: value
        for key, value in fields.items()
        if key in CONTEXT_KEYS and value is not None
    }
    merged = MappingProxyType({**_CONTEXT.get(), **clean})
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_observability_context() -> dict[str, JSONScalar]:
    """返回当前任务的可序列化上下文副本。"""

    return dict(_CONTEXT.get())
