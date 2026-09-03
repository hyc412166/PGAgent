"""Route a model call and recover only from confirmed output boundaries."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
from openai import AsyncOpenAI

from src.config import settings
from src.model.config import ProviderConfig
from src.model.protocols import chat_completions, responses
from src.model.protocols.common import (
    ModelConfigurationError, _as_mapping, _assistant_content, _assistant_reasoning,
    _emit_delta, _normalized_payload,
)
from src.model.retry import pause, request_with_retry
from src.model.streaming import IncompleteResponse, StreamInterrupted


def create_model_call(config: ProviderConfig, *, credentials):
    if config.api_protocol not in {"chat_completions", "responses"}:
        raise ModelConfigurationError(f"不支持的模型协议：{config.api_protocol}")

    async def call(*, messages: list[dict], tools: list[dict], mode: str,
                   on_delta=None, on_thought_delta=None, on_activity=None,
                   prompt_cache_key=None, on_retry=None):
        api_key = credentials(config.secret_ref)
        if not api_key:
            raise ModelConfigurationError("模型连接的 API Key 不存在")
        client = None
        if config.api_protocol == "responses":
            client = AsyncOpenAI(api_key=api_key, base_url=config.base_url.rstrip("/"),
                                 default_headers=config.custom_headers, max_retries=0,
                                 timeout=httpx.Timeout(None, connect=15.0))
        completed: list[dict] = []
        retries = 0
        requests = 0
        try:
            while True:
                current_messages = list(messages)
                if completed:
                    current_messages.append({"role": "assistant", **responses.project_items(completed)})

                async def create():
                    nonlocal requests
                    requests += 1
                    if client is None:
                        return await chat_completions.create(config, api_key, current_messages, tools, mode, prompt_cache_key)
                    response_tool_defs = responses.response_tools(tools)
                    include = ["reasoning.encrypted_content"]
                    if any(tool.get("type") in {"web_search", "web_search_preview"} for tool in response_tool_defs):
                        include.append("web_search_call.action.sources")
                    kwargs: dict[str, Any] = {
                        "model": config.model_id, "input": responses.input_items(current_messages),
                        "tools": response_tool_defs, "stream": True, "store": False,
                        "include": include,
                    }
                    if config.thinking_level not in {"off", "auto", ""} and mode != "compaction":
                        kwargs["reasoning"] = {"effort": config.thinking_level}
                    if prompt_cache_key:
                        kwargs["prompt_cache_key"] = prompt_cache_key
                    return await client.responses.create(**kwargs)

                stream = await request_with_retry(
                    create, retries=settings.model_request_retries,
                    timeout_seconds=settings.model_request_timeout_seconds,
                    on_retry=on_retry, reconnect=mode != "compaction",
                )
                try:
                    if client is not None:
                        if not hasattr(stream, "__aiter__"):
                            raw = _as_mapping(stream, exclude_unset=True)
                            if raw.get("status") != "completed":
                                raise IncompleteResponse("Responses 返回了未完成的响应")
                            payload = responses.project_items(raw.get("output") or [], raw.get("usage"))
                            await _emit_delta(on_thought_delta, payload["reasoning_content"])
                        else:
                            payload = await responses.consume(stream, idle_seconds=settings.model_timeout_seconds,
                                on_delta=on_delta, on_thought_delta=on_thought_delta, on_activity=on_activity)
                        items = [*completed, *payload["_pgagent_provider"]["items"]]
                        payload = responses.project_items(items, payload.get("usage"))
                    elif hasattr(stream, "__aiter__"):
                        payload = await chat_completions.consume(stream, config=config, model=config.model_id,
                            idle_seconds=settings.model_timeout_seconds, on_delta=on_delta,
                            on_thought_delta=on_thought_delta, on_activity=on_activity)
                    else:
                        payload = _as_mapping(stream)
                        finish = (payload.get("choices") or [{}])[0].get("finish_reason")
                        if finish not in {"stop", "tool_calls", "function_call"}:
                            raise IncompleteResponse(f"模型响应未完整结束：{finish}")
                        await _emit_delta(on_thought_delta, _assistant_reasoning(payload))
                        await _emit_delta(on_delta, _assistant_content(payload))
                    normalized = _normalized_payload(payload, config)
                    normalized["usage"]["request_count"] = requests
                    return normalized
                except StreamInterrupted as exc:
                    completed.extend(exc.completed_items)
                    # 已完成的工具调用先交给执行器，绝不以缺失工具结果的历史重新采样。
                    if any(item.get("type") == "function_call" for item in completed):
                        payload = _normalized_payload(responses.project_items(completed), config)
                        payload["usage"]["request_count"] = requests
                        return payload
                    if retries >= settings.model_stream_retries:
                        exc.completed_items = completed
                        raise
                    retries += 1
                    await pause("stream", retries, on_retry)
        finally:
            if client is not None:
                await client.close()

    call.manages_retries = True
    return call
