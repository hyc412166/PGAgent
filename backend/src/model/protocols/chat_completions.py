"""Chat Completions transport and complete-turn assembly."""
from __future__ import annotations
import asyncio
from typing import Any, Mapping
import litellm
import httpx
from src.model.config import ProviderConfig
from src.model.streaming import IncompleteResponse, StreamInterrupted, stream_events
from src.model.protocols.common import (
    PartialModelStreamError, _as_mapping, _litellm_model, _streaming_unsupported, _text_fragment,
    _reasoning_fragment, _tool_call_fragments, _raw_usage, _emit_delta, _emit_activity,
)

async def consume(
    response: Any,
    *,
    config: ProviderConfig,
    model: str,
    idle_seconds: float,
    on_delta: DeltaCallback | None,
    on_thought_delta: DeltaCallback | None,
    on_activity: ActivityCallback | None,
) -> dict[str, Any]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    chunk_count = 0
    visible_output = False
    finished = False
    try:
        async for raw_chunk in stream_events(response, idle_seconds):
            chunk_count += 1
            await _emit_activity(on_activity)
            chunk = _as_mapping(raw_chunk)
            choices = chunk.get("choices") or []
            finish = choices[0].get("finish_reason") if choices else None
            if finish in {"length", "content_filter"}:
                raise IncompleteResponse(f"模型响应未完整结束：{finish}")
            finished = finished or finish in {"stop", "tool_calls", "function_call"}
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
    except IncompleteResponse:
        raise
    except Exception as exc:
        # finish_reason 是服务端的终止边界；其后的 usage 尾块属于可选元数据。
        # 如果只是在读取尾块时断流，保留已确认完整的答案，不重复采样。
        if finished:
            usage.setdefault("_stream_tail_error", type(exc).__name__)
        elif visible_output:
            raise PartialModelStreamError(
                "Chat Completions 事件流在输出可见内容后中断，拒绝自动重试以避免重复输出"
            ) from exc
        else:
            raise StreamInterrupted("Chat Completions 事件流中断") from exc
    if not finished:
        if visible_output:
            raise PartialModelStreamError(
                "Chat Completions 流在输出可见内容后缺少 finish_reason，拒绝自动重试"
            )
        raise StreamInterrupted("Chat Completions 流缺少 finish_reason，不能确认输出完整")
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
    return payload



async def create(config: ProviderConfig, api_key: str, messages: list[dict], tools: list[dict], mode: str, prompt_cache_key: str | None):
    model = _litellm_model(config.provider, config.model_id)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{k: v for k, v in message.items() if not k.startswith("_pgagent_")} for message in messages],
        "tools": tools,
        "api_key": api_key,
        "api_base": config.base_url.rstrip("/"),
        "max_retries": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "drop_params": True,
        "timeout": httpx.Timeout(None, connect=15.0),
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

    return response
