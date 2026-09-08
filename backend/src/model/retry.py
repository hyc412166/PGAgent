"""Request and sampling retries have separate, cancellable budgets."""
# 文件职责：负责模型连接、请求、协议转换与流式响应中的 retry 子模块。
# 逻辑关系：上层通过 model/retry.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
from __future__ import annotations

import asyncio
import inspect
import random
from typing import Any

import httpx

from src.agent.errors import APIErrorKind, classify_api_error


# 函数职责：完成 connection_not_established 对应的业务处理。
# 参数关系：error 表示当前捕获或准备上报的错误。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def connection_not_established(error: BaseException) -> bool:
    # 变量说明：current 表示当前步骤使用的 current 值。
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, httpx.ConnectError):
            return True
        # 变量说明：current 表示当前步骤使用的 current 值。
        current = current.__cause__
    return False


# 函数职责：异步完成 pause 对应的业务处理。
# 参数关系：stage 表示当前步骤使用的 stage 值；attempt 表示当前步骤使用的 attempt 值；callback 表示当前步骤使用的 callback 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def pause(stage: str, attempt: int, callback: Any = None) -> None:
    # 变量说明：delay 表示当前步骤使用的 delay 值。
    delay = min(60.0, 5.0 * 2 ** min(attempt - 1, 4)) if stage == "connection" else 0.2 * 2 ** min(attempt - 1, 8) * random.uniform(0.9, 1.1)
    if callback is not None:
        # 变量说明：result 表示本步骤产生的结果。
        result = callback(stage, attempt, delay)
        if inspect.isawaitable(result):
            await result
    await asyncio.sleep(delay)


# 函数职责：异步完成 request_with_retry 对应的业务处理。
# 参数关系：operation 表示当前步骤使用的 operation 值；retries 表示当前流程使用的 retries 集合；timeout_seconds 表示当前流程使用的 timeout_seconds 集合；on_retry 表示当前步骤使用的 on_retry 值；reconnect 表示当前步骤使用的 reconnect 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def request_with_retry(operation, *, retries: int, timeout_seconds: float, on_retry=None, reconnect: bool = False):
    # 变量说明：attempts 表示当前流程使用的 attempts 集合。
    attempts = 0
    # 变量说明：connection_attempts 表示当前流程使用的 connection_attempts 集合。
    connection_attempts = 0
    while True:
        try:
            async with asyncio.timeout(timeout_seconds):
                return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 变量说明：kind 表示当前步骤使用的 kind 值。
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
