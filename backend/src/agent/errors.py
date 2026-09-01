"""Provider-independent API error classification and retry behavior."""

from __future__ import annotations

import asyncio
import inspect
import random
from enum import StrEnum
from typing import Any, Awaitable, Callable, TypeVar


class APIErrorKind(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN = "unknown"


def status_code_from_error(error: BaseException) -> int | None:
    direct = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    for candidate in (direct, response_status):
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            pass
    return None


def classify_api_error(error: BaseException) -> APIErrorKind:
    status = status_code_from_error(error)
    if status in {401, 403}:
        return APIErrorKind.AUTH
    if status == 429:
        return APIErrorKind.RATE_LIMIT
    if status is not None and 500 <= status <= 599:
        return APIErrorKind.SERVER
    if status is not None and 400 <= status <= 499:
        return APIErrorKind.INVALID_REQUEST

    name = type(error).__name__.lower()
    message = str(error).lower()
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)) or "timeout" in name or "timed out" in message:
        return APIErrorKind.TIMEOUT
    if isinstance(error, (ConnectionError, OSError)) or any(
        word in name or word in message
        for word in ("connection", "connecterror", "network", "dns")
    ):
        return APIErrorKind.CONNECTION
    if any(word in message for word in ("invalid api key", "incorrect api key", "unauthorized", "forbidden")):
        return APIErrorKind.AUTH
    return APIErrorKind.UNKNOWN


def is_retryable_api_error(error: BaseException) -> bool:
    if getattr(error, "retryable", None) is False:
        return False
    return classify_api_error(error) in {
        APIErrorKind.RATE_LIMIT,
        APIErrorKind.SERVER,
        APIErrorKind.CONNECTION,
        APIErrorKind.TIMEOUT,
    }


T = TypeVar("T")


async def call_with_retry(
    operation: Callable[[], T | Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    random_source: Callable[[], float] = random.random,
    on_retry: Callable[[int, float, APIErrorKind, BaseException], Any] | None = None,
) -> T:
    """Call an API with bounded exponential backoff and jitter.

    ``max_attempts`` includes the initial call. Authentication and other 4xx
    failures are surfaced immediately; only 429, 5xx and transport failures retry.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts 必须大于 0")
    for attempt in range(1, max_attempts + 1):
        try:
            result = operation()
            if inspect.isawaitable(result):
                return await result
            return result
        except BaseException as exc:
            if attempt >= max_attempts or not is_retryable_api_error(exc):
                raise
            kind = classify_api_error(exc)
            exponential = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = exponential * (0.75 + 0.5 * random_source())
            if on_retry is not None:
                callback_result = on_retry(attempt, delay, kind, exc)
                if inspect.isawaitable(callback_result):
                    await callback_result
            await sleep(delay)
    raise RuntimeError("unreachable")
