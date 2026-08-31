"""Request and sampling retries have separate, cancellable budgets."""
from __future__ import annotations

import asyncio
import inspect
import random
from typing import Any

import httpx

from src.agent.errors import APIErrorKind, classify_api_error


def connection_not_established(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, httpx.ConnectError):
            return True
        current = current.__cause__
    return False


async def pause(stage: str, attempt: int, callback: Any = None) -> None:
    delay = min(60.0, 5.0 * 2 ** min(attempt - 1, 4)) if stage == "connection" else 0.2 * 2 ** min(attempt - 1, 8) * random.uniform(0.9, 1.1)
    if callback is not None:
        result = callback(stage, attempt, delay)
        if inspect.isawaitable(result):
            await result
    await asyncio.sleep(delay)


async def request_with_retry(operation, *, retries: int, timeout_seconds: float, on_retry=None, reconnect: bool = False):
    attempts = 0
    connection_attempts = 0
    while True:
        try:
            async with asyncio.timeout(timeout_seconds):
                return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            kind = classify_api_error(exc)
            if getattr(exc, "retryable", None) is False:
                raise
            if reconnect and connection_not_established(exc):
                if connection_attempts >= retries:
                    raise
                connection_attempts += 1
                await pause("connection", connection_attempts, on_retry)
                continue
            if kind not in {APIErrorKind.CONNECTION, APIErrorKind.TIMEOUT, APIErrorKind.SERVER} or attempts >= retries:
                raise
            attempts += 1
            await pause("request", attempts, on_retry)
