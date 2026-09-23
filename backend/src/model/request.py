"""Route a model call and recover only from confirmed output boundaries."""
# 文件职责：负责模型连接、请求、协议转换与流式响应中的 request 子模块。
# 逻辑关系：上层通过 model/request.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
from __future__ import annotations

import asyncio
import inspect
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


# 函数职责：创建 model_call 对应的数据或流程。
# 参数关系：config 表示当前生效的配置；credentials 表示当前流程使用的 credentials 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def create_model_call(config: ProviderConfig, *, credentials):
    if config.api_protocol not in {"chat_completions", "responses"}:
        raise ModelConfigurationError(f"不支持的模型协议：{config.api_protocol}")

    # 函数职责：异步完成 call 对应的业务处理。
    # 参数关系：messages 表示发送给模型或客户端的消息序列；tools 表示本轮可调用的工具集合；mode 表示当前步骤使用的 mode 值；on_delta 表示当前步骤使用的 on_delta 值；on_thought_delta 表示当前步骤使用的 on_thought_delta 值；on_activity 表示当前步骤使用的 on_activity 值；prompt_cache_key 表示当前步骤使用的 prompt_cache_key 值；on_retry 表示当前步骤使用的 on_retry 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def call(*, messages: list[dict], tools: list[dict], mode: str,
                   on_delta=None, on_thought_delta=None, on_activity=None,
                   prompt_cache_key=None, on_retry=None, on_assistant_item=None,
                   on_assistant_item_completed=None, on_item=None):
        # 变量说明：api_key 表示当前步骤使用的 api_key 值。
        api_key = credentials(config.secret_ref)
        if not api_key:
            raise ModelConfigurationError("模型连接的 API Key 不存在")
        # 变量说明：client 表示访问外部服务的客户端。
        client = None
        if config.api_protocol == "responses":
            # 变量说明：client 表示访问外部服务的客户端。
            client = AsyncOpenAI(api_key=api_key, base_url=config.base_url.rstrip("/"),
                                 default_headers=config.custom_headers, max_retries=0,
                                 timeout=httpx.Timeout(None, connect=15.0))
        # 变量说明：completed 表示当前步骤使用的 completed 值。
        completed: list[dict] = []
        # 变量说明：retries 表示当前流程使用的 retries 集合。
        retries = 0
        # 变量说明：requests 表示当前流程使用的 requests 集合。
        requests = 0
        try:
            while True:
                # 变量说明：current_messages 表示当前流程使用的 current_messages 集合。
                current_messages = list(messages)
                if completed:
                    current_messages.append({"role": "assistant", **responses.project_items(completed)})

                # 函数职责：异步完成 create 对应的业务处理。
                # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
                async def create():
                    nonlocal requests
                    requests += 1
                    if client is None:
                        return await chat_completions.create(config, api_key, current_messages, tools, mode, prompt_cache_key)
                    # 变量说明：response_tool_defs 表示当前流程使用的 response_tool_defs 集合。
                    response_tool_defs = responses.response_tools(tools)
                    # 变量说明：include 表示当前步骤使用的 include 值。
                    include = ["reasoning.encrypted_content"]
                    if any(tool.get("type") in {"web_search", "web_search_preview"} for tool in response_tool_defs):
                        include.append("web_search_call.action.sources")
                    # 变量说明：kwargs 表示当前流程使用的 kwargs 集合。
                    kwargs: dict[str, Any] = {
                        "model": config.model_id, "input": responses.input_items(current_messages),
                        "tools": response_tool_defs, "stream": True, "store": False,
                        "include": include,
                    }
                    if config.thinking_level not in {"off", "auto", ""} and mode != "compaction":
                        # 变量说明：kwargs 的索引项 表示该语句创建或更新的目标数据。
                        # 请求摘要级 reasoning，Responses 才会发送可展示的增量摘要事件。
                        kwargs["reasoning"] = {"effort": config.thinking_level, "summary": "auto"}
                    if prompt_cache_key:
                        # 变量说明：kwargs 的索引项 表示该语句创建或更新的目标数据。
                        kwargs["prompt_cache_key"] = prompt_cache_key
                    return await client.responses.create(**kwargs)

                # 变量说明：stream 表示当前步骤使用的 stream 值。
                stream = await request_with_retry(
                    create, retries=settings.model_request_retries,
                    timeout_seconds=settings.model_request_timeout_seconds,
                    on_retry=on_retry, reconnect=mode != "compaction",
                )
                try:
                    if client is not None:
                        if not hasattr(stream, "__aiter__"):
                            # 变量说明：raw 表示当前步骤使用的 raw 值。
                            raw = _as_mapping(stream, exclude_unset=True)
                            if raw.get("status") != "completed":
                                raise responses.response_failure_error(
                                    raw,
                                    f"response.{str(raw.get('status') or 'unknown')}",
                                )
                            # 变量说明：payload 表示跨层传递的数据载荷。
                            normalized_response = responses.normalize_response(raw)
                            payload = responses.to_legacy_payload(normalized_response)
                            await _emit_delta(on_thought_delta, payload["reasoning_content"])
                            callback = on_assistant_item or on_item
                            if callback is not None:
                                for item in normalized_response.items:
                                    if item.item_type == "assistant_message":
                                        result = callback(item)
                                        if inspect.isawaitable(result):
                                            await result
                                        if on_assistant_item_completed is not None:
                                            result = on_assistant_item_completed(item)
                                            if inspect.isawaitable(result):
                                                await result
                        else:
                            # 变量说明：payload 表示跨层传递的数据载荷。
                            normalized_response = await responses.consume(stream, idle_seconds=settings.model_timeout_seconds,
                                on_delta=on_delta, on_thought_delta=on_thought_delta,
                                on_activity=on_activity, on_assistant_item=on_assistant_item,
                                on_assistant_item_completed=on_assistant_item_completed,
                                on_item=on_item)
                            payload = responses.to_legacy_payload(normalized_response)
                        # 变量说明：items 表示待处理的元素集合。
                        items = responses.merge_replayed_items(
                            completed,
                            [dict(item) for item in payload["_pgagent_provider"]["items"]],
                        )
                        # 变量说明：payload 表示跨层传递的数据载荷。
                        payload = responses.project_items(items, payload.get("usage"))
                        normalized_response = responses.normalize_response({
                            "id": normalized_response.response_id,
                            "status": "completed",
                            "output": items,
                            "usage": payload.get("usage") or {},
                        })
                    elif hasattr(stream, "__aiter__"):
                        # 变量说明：payload 表示跨层传递的数据载荷。
                        normalized_response = await chat_completions.consume(stream, config=config, model=config.model_id,
                            idle_seconds=settings.model_timeout_seconds, on_delta=on_delta,
                            on_thought_delta=on_thought_delta, on_activity=on_activity,
                            on_assistant_item=on_assistant_item, on_item=on_item)
                        payload = chat_completions.to_legacy_payload(normalized_response)
                    else:
                        # 变量说明：payload 表示跨层传递的数据载荷。
                        payload = _as_mapping(stream)
                        # 变量说明：finish 表示当前步骤使用的 finish 值。
                        finish = (payload.get("choices") or [{}])[0].get("finish_reason")
                        if finish not in {"stop", "tool_calls", "function_call"}:
                            provider_error_code = (
                                "max_output_tokens" if finish == "length" else str(finish or "unknown_finish_reason")
                            )
                            raise IncompleteResponse(
                                f"模型响应未完整结束：{finish}",
                                provider_error_code=provider_error_code,
                            )
                        normalized_response = chat_completions.normalize_response(payload, model=config.model_id)
                        callback = on_assistant_item or on_item
                        if callback is not None:
                            for item in normalized_response.items:
                                if item.item_type == "assistant_message":
                                    result = callback(item)
                                    if inspect.isawaitable(result):
                                        await result
                        payload = chat_completions.to_legacy_payload(normalized_response)
                        await _emit_delta(on_thought_delta, _assistant_reasoning(payload))
                        await _emit_delta(on_delta, _assistant_content(payload))
                    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
                    legacy_response = _normalized_payload(payload, config)
                    legacy_response["usage"]["request_count"] = requests
                    # 旧 engine 继续读取 mapping；新 Turn ledger 可直接消费
                    # adapter 的结构化响应，不需要从扁平文本反推 item。
                    normalized_response.usage = dict(legacy_response["usage"])
                    legacy_response["_pgagent_normalized_response"] = normalized_response
                    return legacy_response
                except StreamInterrupted as exc:
                    completed = responses.merge_replayed_items(
                        completed,
                        [dict(item) for item in exc.completed_items],
                    )
                    # 已完成的工具调用先交给执行器，绝不以缺失工具结果的历史重新采样。
                    if any(item.get("type") == "function_call" for item in completed):
                        # 变量说明：payload 表示跨层传递的数据载荷。
                        payload = _normalized_payload(responses.project_items(completed), config)
                        payload["usage"]["request_count"] = requests
                        interrupted_response = responses.normalize_response({
                            "status": "incomplete",
                            "output": completed,
                            "usage": payload["usage"],
                        })
                        interrupted_response.usage = dict(payload["usage"])
                        payload["_pgagent_normalized_response"] = interrupted_response
                        return payload
                    if retries >= settings.model_stream_retries:
                        # 变量说明：completed_items 表示当前流程使用的 completed_items 集合。
                        exc.completed_items = completed
                        raise
                    retries += 1
                    await pause("stream", retries, on_retry)
        finally:
            if client is not None:
                await client.close()

    # 变量说明：manages_retries 表示当前流程使用的 manages_retries 集合。
    call.manages_retries = True
    # 两种内置协议都会在正文增量后回调带 response/item 身份的累计
    # AssistantMessageItem，运行时据此发布唯一的 canonical SSE 生命周期。
    call.emits_assistant_items = True
    return call
