"""Provider-independent API error classification and retry behavior."""

# 文件职责：统一识别模型供应商异常，并为上层重试策略提供稳定的错误类别。
# 逻辑关系：模型网关捕获原始异常后调用本模块；分类结果决定是否重试以及向运行层暴露何种失败原因。

from __future__ import annotations

import asyncio
import inspect
import random
from enum import StrEnum
from typing import Any, Awaitable, Callable, TypeVar



# 枚举字段把不同 SDK 的异常归一为认证、限流、服务端、连接、超时、请求错误和未知错误。
# 类职责：封装 APIErrorKind 的状态、依赖和领域行为。
# 协作关系：实例由运行服务或相邻节点创建，并在智能体步骤之间传递数据。
class APIErrorKind(StrEnum):
    # 变量说明：AUTH 表示当前步骤使用的 AUTH 值。
    AUTH = "auth"
    # 变量说明：RATE_LIMIT 表示当前步骤使用的 RATE_LIMIT 值。
    RATE_LIMIT = "rate_limit"
    # 变量说明：SERVER 表示当前步骤使用的 SERVER 值。
    SERVER = "server"
    # 变量说明：CONNECTION 表示当前步骤使用的 CONNECTION 值。
    CONNECTION = "connection"
    # 变量说明：TIMEOUT 表示当前步骤使用的 TIMEOUT 值。
    TIMEOUT = "timeout"
    # 变量说明：INVALID_REQUEST 表示当前步骤使用的 INVALID_REQUEST 值。
    INVALID_REQUEST = "invalid_request"
    # 变量说明：UNKNOWN 表示当前步骤使用的 UNKNOWN 值。
    UNKNOWN = "unknown"


_PROVIDER_RATE_LIMIT_CODES = {"rate_limit_exceeded"}
_PROVIDER_SERVER_CODES = {"server_error", "server_is_overloaded"}
_PROVIDER_TIMEOUT_CODES = {"vector_store_timeout"}
_PROVIDER_INVALID_REQUEST_CODES = {
    "bio_policy",
    "data_residency_mismatch",
    "empty_image_file",
    "failed_to_download_image",
    "image_content_policy_violation",
    "image_file_not_found",
    "image_file_too_large",
    "image_parse_error",
    "image_too_large",
    "image_too_small",
    "invalid_base64_image",
    "invalid_image",
    "invalid_image_format",
    "invalid_image_mode",
    "invalid_image_url",
    "invalid_prompt",
    "unsupported_image_media_type",
}



# 从异常本身或其 response 对象提取 HTTP 状态码；无法转换时返回 None。
# 函数职责：完成 status_code_from_error 对应的智能体处理。
# 参数关系：error 表示当前异常。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
def status_code_from_error(error: BaseException) -> int | None:
    # 变量说明：direct 表示当前步骤使用的 direct 值。
    direct = getattr(error, "status_code", None)
    # 变量说明：response 表示下游响应。
    response = getattr(error, "response", None)
    # 变量说明：response_status 表示response_status 集合。
    response_status = getattr(response, "status_code", None)
    for candidate in (direct, response_status):
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            pass
    return None



# 结合状态码、异常类型和消息文本，将供应商异常映射为统一类别。
# 函数职责：完成 classify_api_error 对应的智能体处理。
# 参数关系：error 表示当前异常。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
def classify_api_error(error: BaseException) -> APIErrorKind:
    provider_code = str(getattr(error, "provider_error_code", None) or "").casefold()
    provider_type = str(getattr(error, "provider_error_type", None) or "").casefold()
    if provider_code in _PROVIDER_RATE_LIMIT_CODES or provider_type == "rate_limit_error":
        return APIErrorKind.RATE_LIMIT
    if provider_code in _PROVIDER_TIMEOUT_CODES:
        return APIErrorKind.TIMEOUT
    if (
        provider_code in _PROVIDER_SERVER_CODES
        or provider_type in {"server_error", "service_unavailable_error"}
    ):
        return APIErrorKind.SERVER
    if provider_code in _PROVIDER_INVALID_REQUEST_CODES or provider_type == "invalid_request_error":
        return APIErrorKind.INVALID_REQUEST
    # 变量说明：status 表示status 集合。
    status = status_code_from_error(error)
    if status in {401, 403}:
        return APIErrorKind.AUTH
    if status == 429:
        return APIErrorKind.RATE_LIMIT
    if status is not None and 500 <= status <= 599:
        return APIErrorKind.SERVER
    if status is not None and 400 <= status <= 499:
        return APIErrorKind.INVALID_REQUEST

    # 变量说明：name 表示当前步骤使用的 name 值。
    name = type(error).__name__.lower()
    # 变量说明：message 表示当前步骤使用的 message 值。
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



# 判断错误是否适合自动重试；异常显式声明 retryable=False 时优先尊重该约束。
# 函数职责：判断是否 retryable_api_error 对应流程。
# 参数关系：error 表示当前异常。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
def is_retryable_api_error(error: BaseException) -> bool:
    if getattr(error, "retryable", None) is False:
        return False
    return classify_api_error(error) in {
        APIErrorKind.RATE_LIMIT,
        APIErrorKind.SERVER,
        APIErrorKind.CONNECTION,
        APIErrorKind.TIMEOUT,
    }


# T 表示被重试操作最终返回的任意结果类型。
# 变量说明：T 表示当前步骤使用的 T 值。
T = TypeVar("T")


# 按指数退避执行同步或异步操作，并仅重试可恢复的供应商错误。
# 函数职责：异步完成 call_with_retry 对应的智能体处理。
# 参数关系：operation 表示当前步骤使用的 operation 值；max_attempts 表示max_attempts 集合；base_delay 表示当前步骤使用的 base_delay 值；max_delay 表示当前步骤使用的 max_delay 值；sleep 表示当前步骤使用的 sleep 值；random_source 表示当前步骤使用的 random_source 值；on_retry 表示当前步骤使用的 on_retry 值。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
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
            # 变量说明：result 表示本步骤处理结果。
            result = operation()
            if inspect.isawaitable(result):
                return await result
            return result
        except BaseException as exc:
            if attempt >= max_attempts or not is_retryable_api_error(exc):
                raise
            # 变量说明：kind 表示当前步骤使用的 kind 值。
            kind = classify_api_error(exc)
            # 变量说明：exponential 表示当前步骤使用的 exponential 值。
            exponential = min(max_delay, base_delay * (2 ** (attempt - 1)))
            # 变量说明：delay 表示当前步骤使用的 delay 值。
            delay = exponential * (0.75 + 0.5 * random_source())
            if on_retry is not None:
                # 变量说明：callback_result 表示当前步骤使用的 callback_result 值。
                callback_result = on_retry(attempt, delay, kind, exc)
                if inspect.isawaitable(callback_result):
                    await callback_result
            await sleep(delay)
    raise RuntimeError("unreachable")
