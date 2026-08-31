"""Stream lifetime and partial-result boundaries shared by wire adapters."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, AsyncIterator


class StreamInterrupted(ConnectionError):
    retryable = True

    def __init__(self, message: str, *, completed_items: list[dict] | None = None):
        super().__init__(message)
        self.completed_items = list(completed_items or [])


class IncompleteResponse(RuntimeError):
    retryable = False


async def stream_events(stream: Any, idle_seconds: float) -> AsyncIterator[Any]:
    iterator = stream.__aiter__()
    try:
        while True:
            try:
                event = await asyncio.wait_for(anext(iterator), timeout=idle_seconds)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as exc:
                raise StreamInterrupted("模型事件流连续无活动，等待超时") from exc
            yield event
    finally:
        close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
