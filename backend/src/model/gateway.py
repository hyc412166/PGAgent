"""LiteLLM-backed provider adapter used by the agent runtime."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

import litellm

from src.agent import normalize_usage
from src.model.credentials import get_api_key


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
        # LiteLLM's DeepSeek adapter still flattens list content to text and
        # drops image_url blocks. DeepSeek's vision endpoint uses the standard
        # OpenAI Chat Completions shape, so route this model through that
        # transport until the adapter supports multimodal content directly.
        if model_id.removeprefix("deepseek/") == "deepseek-v4-flash-vision-exp":
            return f"openai/{model_id.removeprefix('deepseek/')}"
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


async def _consume_stream(
    response: Any,
    *,
    config: ProviderConfig,
    model: str,
    on_delta: DeltaCallback | None,
    on_thought_delta: DeltaCallback | None,
) -> dict[str, Any]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
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
            reasoning_fragment = _reasoning_fragment(chunk)
            if reasoning_fragment:
                reasoning_parts.append(reasoning_fragment)
                await _emit_delta(on_thought_delta, reasoning_fragment)
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
                "reasoning_content": "".join(reasoning_parts),
                "tool_calls": assembled_calls,
            }
        }],
        "usage": usage,
    }
    return _normalized_payload(payload, config)


def _hydrate_attachment_messages(
    messages: list[dict[str, Any]],
    attachment_store: Any | None,
) -> list[dict[str, Any]]:
    hydrated: list[dict[str, Any]] = []
    pending_tool_images: list[Mapping[str, Any]] = []
    for index, message in enumerate(messages):
        provider_message = {
            key: value for key, value in message.items()
            if not str(key).startswith("_pgagent_")
        }
        content = message.get("content")
        if not isinstance(content, list):
            hydrated.append(provider_message)
        else:
            parts: list[dict[str, Any]] = []
            for raw_part in content:
                if not isinstance(raw_part, Mapping) or raw_part.get("type") != "pgagent_image_ref":
                    parts.append(dict(raw_part) if isinstance(raw_part, Mapping) else {"type": "text", "text": str(raw_part)})
                    continue
                attachment_id = str(raw_part.get("attachment_id") or "")
                data_url = attachment_store.data_url(attachment_id) if attachment_store is not None else None
                if not data_url:
                    raise ModelConfigurationError("会话图片附件不可用，无法安全构造模型输入")
                parts.append({"type": "image_url", "image_url": {"url": data_url}})
            hydrated.append({**provider_message, "content": parts})

        if message.get("role") == "tool" and isinstance(content, str):
            try:
                tool_payload = json.loads(content)
            except (TypeError, ValueError):
                tool_payload = {}
            metadata = tool_payload.get("metadata") if isinstance(tool_payload, Mapping) else None
            image_ref = metadata.get("model_image_ref") if isinstance(metadata, Mapping) else None
            if isinstance(image_ref, Mapping) and str(image_ref.get("id") or ""):
                pending_tool_images.append(image_ref)

        next_is_tool = (
            index + 1 < len(messages)
            and messages[index + 1].get("role") == "tool"
        )
        if pending_tool_images and not next_is_tool:
            observation_parts: list[dict[str, Any]] = [{
                "type": "text",
                "text": "附件工具生成了以下会话内图片，请直接观察图片继续分析。",
            }]
            for image_ref in pending_tool_images:
                attachment_id = str(image_ref.get("id") or "")
                data_url = attachment_store.data_url(attachment_id) if attachment_store is not None else None
                if not data_url:
                    raise ModelConfigurationError("PDF 派生图片不可用，无法安全构造模型输入")
                observation_parts.append({"type": "image_url", "image_url": {"url": data_url}})
            hydrated.append({"role": "user", "content": observation_parts})
            pending_tool_images.clear()
    return hydrated


def bind_attachment_store(model_call: Any, attachment_store: Any | None) -> Any:
    """Hydrate session-private image refs without changing provider factory APIs."""

    if attachment_store is None:
        return model_call

    async def call_with_attachments(**kwargs: Any) -> Any:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            kwargs = {
                **kwargs,
                "messages": _hydrate_attachment_messages(messages, attachment_store),
            }
        return await model_call(**kwargs)

    return call_with_attachments


def build_model_call(config: ProviderConfig):
    """Create the injected callable expected by :class:`AgentRuntime`."""

    async def call(
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        mode: str,
        on_delta: DeltaCallback | None = None,
        on_thought_delta: DeltaCallback | None = None,
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
                on_thought_delta=on_thought_delta,
            )

        # Some OpenAI-compatible relays silently ignore stream=True and return
        # a normal completion. Treat that as an explicit safe one-shot fallback.
        payload = _normalized_payload(_as_mapping(response), config)
        await _emit_delta(on_thought_delta, _assistant_reasoning(payload))
        await _emit_delta(on_delta, _assistant_content(payload))
        return payload

    return call
