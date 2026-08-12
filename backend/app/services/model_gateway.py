"""LiteLLM-backed provider adapter used by the agent runtime."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

import litellm

from app.runtime import normalize_usage
from app.secrets import get_api_key


DeltaCallback = Callable[[str], Any | Awaitable[Any]]


@dataclass(slots=True)
class ProviderConfig:
    provider: str
    base_url: str
    secret_ref: str
    model_id: str
    model_connection_id: str | None = None
    thinking_level: str = "auto"
    custom_headers: dict[str, str] = field(default_factory=dict)
    # Keep provider turns bounded even when a relay defaults to a very large
    # completion window.  Context budgeting reserves the same order of room;
    # callers can override this per connection in a future settings surface.
    max_output_tokens: int = 8_000


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
        return model_id if model_id.startswith("deepseek/") else f"deepseek/{model_id}"
    return model_id if model_id.startswith("openai/") else f"openai/{model_id}"


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        payload = value.model_dump()
        if isinstance(payload, Mapping):
            return dict(payload)
    raise TypeError("LiteLLM response must be a mapping or support model_dump")


async def _emit_delta(callback: DeltaCallback | None, delta: str) -> None:
    if callback is None or not delta:
        return
    result = callback(delta)
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


async def _consume_stream(
    response: Any,
    *,
    config: ProviderConfig,
    model: str,
    on_delta: DeltaCallback | None,
) -> dict[str, Any]:
    content_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    chunk_count = 0
    visible_output = False
    try:
        async for raw_chunk in response:
            chunk_count += 1
            chunk = _as_mapping(raw_chunk)
            fragment = _text_fragment(chunk)
            if fragment:
                content_parts.append(fragment)
                await _emit_delta(on_delta, fragment)
                visible_output = True
            for fallback_index, raw_call in enumerate(_tool_call_fragments(chunk)):
                raw_index = raw_call.get("index", fallback_index)
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    index = fallback_index
                current = tool_calls.setdefault(index, {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                call_id = raw_call.get("id")
                if call_id:
                    current["id"] = str(call_id)
                call_type = raw_call.get("type")
                if call_type:
                    current["type"] = str(call_type)
                function = raw_call.get("function") or {}
                if not isinstance(function, Mapping):
                    function = _as_mapping(function)
                name = function.get("name")
                if name:
                    current["function"]["name"] += str(name)
                arguments = function.get("arguments")
                if arguments:
                    current["function"]["arguments"] += str(arguments)
            chunk_usage = _raw_usage(chunk)
            if chunk_usage:
                usage.update(chunk_usage)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        if visible_output:
            raise PartialModelStreamError(str(exc) or type(exc).__name__) from exc
        raise
    finally:
        close = getattr(response, "aclose", None)
        if callable(close):
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result

    if chunk_count == 0:
        raise RuntimeError("LiteLLM streaming response produced no chunks")
    assembled_calls: list[dict[str, Any]] = []
    for index, item in sorted(tool_calls.items()):
        assembled_calls.append({
            "id": item["id"] or f"call-{index + 1}",
            "type": item["type"],
            "function": dict(item["function"]),
        })
    payload: dict[str, Any] = {
        "model": model,
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "".join(content_parts),
                "tool_calls": assembled_calls,
            }
        }],
        "usage": usage,
    }
    return _normalized_payload(payload, config)


def build_model_call(config: ProviderConfig):
    """Create the injected callable expected by :class:`AgentRuntime`."""

    async def call(
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        mode: str,
        on_delta: DeltaCallback | None = None,
        prompt_cache_key: str | None = None,
    ) -> Any:
        api_key = get_api_key(config.secret_ref)
        if not api_key:
            raise ModelConfigurationError("The model connection API Key is missing")

        model = _litellm_model(config.provider, config.model_id)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "api_key": api_key,
            "api_base": config.base_url.rstrip("/"),
            "max_retries": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
            "drop_params": True,
        }
        output_limit = 2_000 if mode == "compaction" else config.max_output_tokens
        if output_limit > 0:
            kwargs["max_tokens"] = int(output_limit)
        if prompt_cache_key:
            # LiteLLM forwards this to OpenAI-compatible providers that support
            # explicit prompt caching.  Providers that do not support it safely
            # ignore it through ``drop_params``.
            kwargs["prompt_cache_key"] = prompt_cache_key
        if config.custom_headers:
            kwargs["extra_headers"] = dict(config.custom_headers)
        if mode != "compaction" and config.thinking_level not in {"off", "auto", ""}:
            kwargs["reasoning_effort"] = config.thinking_level

        try:
            response = await litellm.acompletion(**kwargs)
        except BaseException as exc:
            if not _streaming_unsupported(exc):
                raise
            fallback_kwargs = {key: value for key, value in kwargs.items() if key != "stream_options"}
            fallback_kwargs["stream"] = False
            response = await litellm.acompletion(**fallback_kwargs)

        if hasattr(response, "__aiter__"):
            return await _consume_stream(
                response,
                config=config,
                model=model,
                on_delta=on_delta,
            )

        # Some OpenAI-compatible relays silently ignore stream=True and return
        # a normal completion. Treat that as an explicit safe one-shot fallback.
        payload = _normalized_payload(_as_mapping(response), config)
        await _emit_delta(on_delta, _assistant_content(payload))
        return payload

    return call
