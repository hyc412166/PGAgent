# 文件职责：负责模型连接、请求、协议转换与流式响应中的 common 子模块。
# 逻辑关系：上层通过 model/protocols/common.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Mapping

import litellm
from src.agent import normalize_usage
from src.model.config import ProviderConfig

# 变量说明：DeltaCallback 表示当前步骤使用的 DeltaCallback 值。
DeltaCallback = Callable[[str], Any | Awaitable[Any]]
# 变量说明：ActivityCallback 表示当前步骤使用的 ActivityCallback 值。
ActivityCallback = Callable[[], Any | Awaitable[Any]]

# 类职责：表示 ModelConfigurationError 场景的领域异常。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ModelConfigurationError(RuntimeError):
    """Raised when a session has no usable model connection."""


# 类职责：表示 PartialModelStreamError 场景的领域异常。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class PartialModelStreamError(RuntimeError):
    """A stream failed after visible output, so retrying would duplicate text."""

    # 变量说明：retryable 表示当前步骤使用的 retryable 值。
    retryable = False


# 函数职责：完成 litellm_model 对应的业务处理。
# 参数关系：provider 表示模型供应商；model_id 表示model 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _litellm_model(provider: str, model_id: str) -> str:
    """Map UI provider names to LiteLLM's provider/model convention."""

    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = provider.strip().lower()
    if normalized == "openrouter":
        return model_id if model_id.startswith("openrouter/") else f"openrouter/{model_id}"
    if normalized == "deepseek":
        # LiteLLM's DeepSeek adapter still flattens list content to text and
        # drops image_url blocks. DeepSeek's vision endpoint uses the standard
        # OpenAI Chat Completions shape, so route this model through that
        # transport until the adapter supports multimodal content directly.
        if model_id.removeprefix("deepseek/") == "deepseek-v4-flash-vision-exp":
            return f"openai/{model_id.removeprefix('deepseek/')}"
        return model_id if model_id.startswith("deepseek/") else f"deepseek/{model_id}"
    return model_id if model_id.startswith("openai/") else f"openai/{model_id}"


# 函数职责：完成 as_mapping 对应的业务处理。
# 参数关系：value 表示当前字段或计算值；exclude_unset 表示当前步骤使用的 exclude_unset 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _as_mapping(value: Any, *, exclude_unset: bool = False) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = value.model_dump(exclude_unset=True) if exclude_unset else value.model_dump()
        if isinstance(payload, Mapping):
            return dict(payload)
    raise TypeError("LiteLLM response must be a mapping or support model_dump")


# 函数职责：异步发送 delta 对应的数据或流程。
# 参数关系：callback 表示当前步骤使用的 callback 值；delta 表示当前步骤使用的 delta 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def _emit_delta(callback: DeltaCallback | None, delta: str) -> None:
    if callback is None or not delta:
        return
    # 变量说明：result 表示本步骤产生的结果。
    result = callback(delta)
    if inspect.isawaitable(result):
        await result


# 函数职责：异步发送 activity 对应的数据或流程。
# 参数关系：callback 表示当前步骤使用的 callback 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def _emit_activity(callback: ActivityCallback | None) -> None:
    if callback is None:
        return
    # 变量说明：result 表示本步骤产生的结果。
    result = callback()
    if inspect.isawaitable(result):
        await result


# 函数职责：完成 assistant_content 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _assistant_content(payload: Mapping[str, Any]) -> str:
    # 变量说明：choices 表示当前流程使用的 choices 集合。
    choices = payload.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    # 变量说明：choice 表示当前步骤使用的 choice 值。
    choice = choices[0]
    if not isinstance(choice, Mapping):
        # 变量说明：choice 表示当前步骤使用的 choice 值。
        choice = _as_mapping(choice)
    # 变量说明：message 表示当前消息。
    message = choice.get("message") or {}
    if not isinstance(message, Mapping):
        # 变量说明：message 表示当前消息。
        message = _as_mapping(message)
    # 变量说明：content 表示待处理或返回的正文内容。
    content = message.get("content")
    return content if isinstance(content, str) else ""


# 函数职责：完成 streaming_unsupported 对应的业务处理。
# 参数关系：error 表示当前捕获或准备上报的错误。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _streaming_unsupported(error: BaseException) -> bool:
    """Recognize explicit provider rejections without hiding real API errors."""

    # 变量说明：status_code 表示当前步骤使用的 status_code 值。
    status_code = getattr(error, "status_code", None)
    try:
        # 变量说明：invalid_request 表示当前步骤使用的 invalid_request 值。
        invalid_request = status_code is not None and int(status_code) in {400, 404, 405, 415, 422, 501}
    except (TypeError, ValueError):
        # 变量说明：invalid_request 表示当前步骤使用的 invalid_request 值。
        invalid_request = False
    # 变量说明：message 表示当前消息。
    message = str(error).lower()
    # 变量说明：rejection 表示当前步骤使用的 rejection 值。
    rejection = any(
        marker in message
        for marker in (
            "not support",
            "unsupported",
            "does not support",
            "unknown parameter",
            "unrecognized parameter",
            "extra fields not permitted",
        )
    )
    return invalid_request and "stream" in message and rejection


# 函数职责：完成 tool_call_fragments 对应的业务处理。
# 参数关系：chunk 表示当前步骤使用的 chunk 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _tool_call_fragments(chunk: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    # 变量说明：choices 表示当前流程使用的 choices 集合。
    choices = chunk.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return []
    # 变量说明：choice 表示当前步骤使用的 choice 值。
    choice = choices[0]
    if not isinstance(choice, Mapping):
        # 变量说明：choice 表示当前步骤使用的 choice 值。
        choice = _as_mapping(choice)
    # 变量说明：delta 表示当前步骤使用的 delta 值。
    delta = choice.get("delta") or {}
    if not isinstance(delta, Mapping):
        # 变量说明：delta 表示当前步骤使用的 delta 值。
        delta = _as_mapping(delta)
    # 变量说明：calls 表示当前流程使用的 calls 集合。
    calls = delta.get("tool_calls") or []
    return [item if isinstance(item, Mapping) else _as_mapping(item) for item in calls]


# 函数职责：完成 text_fragment 对应的业务处理。
# 参数关系：chunk 表示当前步骤使用的 chunk 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _text_fragment(chunk: Mapping[str, Any]) -> str:
    # 变量说明：choices 表示当前流程使用的 choices 集合。
    choices = chunk.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    # 变量说明：choice 表示当前步骤使用的 choice 值。
    choice = choices[0]
    if not isinstance(choice, Mapping):
        # 变量说明：choice 表示当前步骤使用的 choice 值。
        choice = _as_mapping(choice)
    # 变量说明：delta 表示当前步骤使用的 delta 值。
    delta = choice.get("delta") or {}
    if not isinstance(delta, Mapping):
        # 变量说明：delta 表示当前步骤使用的 delta 值。
        delta = _as_mapping(delta)
    # 变量说明：content 表示待处理或返回的正文内容。
    content = delta.get("content")
    return content if isinstance(content, str) else ""


# 函数职责：完成 reasoning_fragment 对应的业务处理。
# 参数关系：chunk 表示当前步骤使用的 chunk 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _reasoning_fragment(chunk: Mapping[str, Any]) -> str:
    """Extract provider reasoning without mixing it into answer content."""

    # 变量说明：choices 表示当前流程使用的 choices 集合。
    choices = chunk.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    # 变量说明：choice 表示当前步骤使用的 choice 值。
    choice = choices[0]
    if not isinstance(choice, Mapping):
        # 变量说明：choice 表示当前步骤使用的 choice 值。
        choice = _as_mapping(choice)
    # 变量说明：delta 表示当前步骤使用的 delta 值。
    delta = choice.get("delta") or {}
    if not isinstance(delta, Mapping):
        # 变量说明：delta 表示当前步骤使用的 delta 值。
        delta = _as_mapping(delta)
    for key in ("reasoning_content", "reasoning", "thinking"):
        # 变量说明：value 表示当前字段或计算值。
        value = delta.get(key)
        if isinstance(value, str):
            return value
    return ""


# 函数职责：完成 assistant_reasoning 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _assistant_reasoning(payload: Mapping[str, Any]) -> str:
    # 变量说明：choices 表示当前流程使用的 choices 集合。
    choices = payload.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    # 变量说明：choice 表示当前步骤使用的 choice 值。
    choice = choices[0]
    if not isinstance(choice, Mapping):
        # 变量说明：choice 表示当前步骤使用的 choice 值。
        choice = _as_mapping(choice)
    # 变量说明：message 表示当前消息。
    message = choice.get("message") or {}
    if not isinstance(message, Mapping):
        # 变量说明：message 表示当前消息。
        message = _as_mapping(message)
    for key in ("reasoning_content", "reasoning", "thinking"):
        # 变量说明：value 表示当前字段或计算值。
        value = message.get(key)
        if isinstance(value, str):
            return value
    return ""


# 函数职责：完成 raw_usage 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _raw_usage(payload: Mapping[str, Any]) -> dict[str, Any]:
    # 变量说明：usage 表示当前步骤使用的 usage 值。
    usage = payload.get("usage") or {}
    if isinstance(usage, Mapping):
        return dict(usage)
    if hasattr(usage, "model_dump"):
        # 变量说明：dumped 表示当前步骤使用的 dumped 值。
        dumped = usage.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


# 函数职责：完成 normalized_payload 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷；config 表示当前生效的配置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _normalized_payload(payload: dict[str, Any], config: ProviderConfig) -> dict[str, Any]:
    try:
        # 变量说明：cost_usd 表示当前步骤使用的 cost_usd 值。
        cost_usd = float(litellm.completion_cost(completion_response=payload) or 0.0)
    except Exception:
        # Custom and newly released models may not have a LiteLLM price table.
        # 变量说明：cost_usd 表示当前步骤使用的 cost_usd 值。
        cost_usd = 0.0
    payload["usage"] = normalize_usage({
        **_raw_usage(payload),
        "request_count": 1,
        "cost_usd": cost_usd,
        "model_connection_id": config.model_connection_id,
        "model_id": config.model_id,
        "provider": config.provider,
    })
    return payload
