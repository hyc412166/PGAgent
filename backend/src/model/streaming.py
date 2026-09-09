"""Stream lifetime and partial-result boundaries shared by wire adapters."""
# 文件职责：负责模型连接、请求、协议转换与流式响应中的 streaming 子模块。
# 逻辑关系：上层通过 model/streaming.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
from __future__ import annotations

import asyncio
import inspect
from typing import Any, AsyncIterator


# 类职责：定义 StreamInterrupted 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class StreamInterrupted(ConnectionError):
    # 变量说明：retryable 表示当前步骤使用的 retryable 值。
    retryable = True

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：message 表示当前消息；completed_items 表示当前流程使用的 completed_items 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(
        self,
        message: str,
        *,
        completed_items: list[dict] | None = None,
        provider_error_code: str | None = None,
        provider_error_type: str | None = None,
    ):
        super().__init__(message)
        # 变量说明：completed_items 表示当前流程使用的 completed_items 集合。
        self.completed_items = list(completed_items or [])
        self.provider_error_code = str(provider_error_code or "").strip() or None
        self.provider_error_type = str(provider_error_type or "").strip() or None


# 类职责：定义 IncompleteResponse 的跨层数据契约。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class IncompleteResponse(RuntimeError):
    # 变量说明：retryable 表示当前步骤使用的 retryable 值。
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider_error_code: str | None = None,
        provider_error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_error_code = str(provider_error_code or "").strip() or None
        self.provider_error_type = str(provider_error_type or "").strip() or None


# 函数职责：异步流式传输 events 对应的数据或流程。
# 参数关系：stream 表示当前步骤使用的 stream 值；idle_seconds 表示当前流程使用的 idle_seconds 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def stream_events(stream: Any, idle_seconds: float) -> AsyncIterator[Any]:
    # 变量说明：iterator 表示当前步骤使用的 iterator 值。
    iterator = stream.__aiter__()
    try:
        while True:
            try:
                # 变量说明：event 表示当前运行事件。
                event = await asyncio.wait_for(anext(iterator), timeout=idle_seconds)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as exc:
                raise StreamInterrupted("模型事件流连续无活动，等待超时") from exc
            yield event
    finally:
        # 变量说明：close 表示当前步骤使用的 close 值。
        close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
        if close is not None:
            # 变量说明：result 表示本步骤产生的结果。
            result = close()
            if inspect.isawaitable(result):
                await result
