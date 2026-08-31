from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Mapping

import litellm
from src.agent import normalize_usage
from src.model.config import ProviderConfig

DeltaCallback = Callable[[str], Any | Awaitable[Any]]
ActivityCallback = Callable[[], Any | Awaitable[Any]]

class ModelConfigurationError(RuntimeError):
    """Raised when a session has no usable model connection."""


class PartialModelStreamError(RuntimeError):
    """A stream failed after visible output, so retrying would duplicate text."""

    retryable = False


def _litellm_model(provider: str, model_id: str) -> str:
    """Map UI provider names to LiteLLM's provider/model convention."""

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


def _as_mapping(value: Any, *, exclude_unset: bool = False) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        payload = value.model_dump(exclude_unset=True) if exclude_unset else value.model_dump()
        if isinstance(payload, Mapping):
            return dict(payload)
    raise TypeError("LiteLLM response must be a mapping or support model_dump")


async def _emit_delta(callback: DeltaCallback | None, delta: str) -> None:
    if callback is None or not delta:
        return
    result = callback(delta)
    if inspect.isawaitable(result):
        await result


async def _emit_activity(callback: ActivityCallback | None) -> None:
    if callback is None:
        return
    result = callback()
    if inspect.isawaitable(result):
        await result


def _assistant_content(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, Mapping):
        choice = _as_mapping(choice)
    message = choice.get("message") or {}
    if not isinstance(message, Mapping):
        message = _as_mapping(message)
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _streaming_unsupported(error: BaseException) -> bool:
    """Recognize explicit provider rejections without hiding real API errors."""

    status_code = getattr(error, "status_code", None)
    try:
        invalid_request = status_code is not None and int(status_code) in {400, 404, 405, 415, 422, 501}
    except (TypeError, ValueError):
        invalid_request = False
    message = str(error).lower()
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


def _tool_call_fragments(chunk: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    choices = chunk.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return []
    choice = choices[0]
    if not isinstance(choice, Mapping):
        choice = _as_mapping(choice)
    delta = choice.get("delta") or {}
    if not isinstance(delta, Mapping):
        delta = _as_mapping(delta)
    calls = delta.get("tool_calls") or []
    return [item if isinstance(item, Mapping) else _as_mapping(item) for item in calls]


def _text_fragment(chunk: Mapping[str, Any]) -> str:
    choices = chunk.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, Mapping):
        choice = _as_mapping(choice)
    delta = choice.get("delta") or {}
    if not isinstance(delta, Mapping):
        delta = _as_mapping(delta)
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _reasoning_fragment(chunk: Mapping[str, Any]) -> str:
    """Extract provider reasoning without mixing it into answer content."""

    choices = chunk.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, Mapping):
        choice = _as_mapping(choice)
    delta = choice.get("delta") or {}
    if not isinstance(delta, Mapping):
        delta = _as_mapping(delta)
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = delta.get(key)
        if isinstance(value, str):
            return value
    return ""


def _assistant_reasoning(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, Mapping):
        choice = _as_mapping(choice)
    message = choice.get("message") or {}
    if not isinstance(message, Mapping):
        message = _as_mapping(message)
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str):
            return value
    return ""


def _raw_usage(payload: Mapping[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage") or {}
    if isinstance(usage, Mapping):
        return dict(usage)
    if hasattr(usage, "model_dump"):
        dumped = usage.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _normalized_payload(payload: dict[str, Any], config: ProviderConfig) -> dict[str, Any]:
    try:
        cost_usd = float(litellm.completion_cost(completion_response=payload) or 0.0)
    except Exception:
        # Custom and newly released models may not have a LiteLLM price table.
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
