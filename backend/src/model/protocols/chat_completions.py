"""Chat Completions transport and complete-turn assembly."""
# 文件职责：负责模型连接、请求、协议转换与流式响应中的 chat_completions 子模块。
# 逻辑关系：上层通过 model/protocols/chat_completions.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
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

# 函数职责：异步完成 consume 对应的业务处理。
# 参数关系：response 表示下游返回的响应；config 表示当前生效的配置；model 表示当前选择的模型；idle_seconds 表示当前流程使用的 idle_seconds 集合；on_delta 表示当前步骤使用的 on_delta 值；on_thought_delta 表示当前步骤使用的 on_thought_delta 值；on_activity 表示当前步骤使用的 on_activity 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：content_parts 表示当前流程使用的 content_parts 集合。
    content_parts: list[str] = []
    # 变量说明：reasoning_parts 表示当前流程使用的 reasoning_parts 集合。
    reasoning_parts: list[str] = []
    # 变量说明：tool_calls 表示当前流程使用的 tool_calls 集合。
    tool_calls: dict[int, dict[str, Any]] = {}
    # 变量说明：usage 表示当前步骤使用的 usage 值。
    usage: dict[str, Any] = {}
    # 变量说明：chunk_count 表示chunk 的数量。
    chunk_count = 0
    # 变量说明：visible_output 表示当前步骤使用的 visible_output 值。
    visible_output = False
    # 变量说明：finished 表示当前步骤使用的 finished 值。
    finished = False
    try:
        async for raw_chunk in stream_events(response, idle_seconds):
            chunk_count += 1
            await _emit_activity(on_activity)
            # 变量说明：chunk 表示当前步骤使用的 chunk 值。
            chunk = _as_mapping(raw_chunk)
            # 变量说明：choices 表示当前流程使用的 choices 集合。
            choices = chunk.get("choices") or []
            # 变量说明：finish 表示当前步骤使用的 finish 值。
            finish = choices[0].get("finish_reason") if choices else None
            if finish in {"length", "content_filter"}:
                raise IncompleteResponse(f"模型响应未完整结束：{finish}")
            # 变量说明：finished 表示当前步骤使用的 finished 值。
            finished = finished or finish in {"stop", "tool_calls", "function_call"}
            # 变量说明：fragment 表示当前步骤使用的 fragment 值。
            fragment = _text_fragment(chunk)
            if fragment:
                content_parts.append(fragment)
                await _emit_delta(on_delta, fragment)
                # 变量说明：visible_output 表示当前步骤使用的 visible_output 值。
                visible_output = True
            # 变量说明：reasoning_fragment 表示当前步骤使用的 reasoning_fragment 值。
            reasoning_fragment = _reasoning_fragment(chunk)
            if reasoning_fragment:
                reasoning_parts.append(reasoning_fragment)
                await _emit_delta(on_thought_delta, reasoning_fragment)
                # 变量说明：visible_output 表示当前步骤使用的 visible_output 值。
                visible_output = True
            for fallback_index, raw_call in enumerate(_tool_call_fragments(chunk)):
                # 变量说明：raw_index 表示当前步骤使用的 raw_index 值。
                raw_index = raw_call.get("index", fallback_index)
                try:
                    # 变量说明：index 表示当前元素的位置索引。
                    index = int(raw_index)
                except (TypeError, ValueError):
                    # 变量说明：index 表示当前元素的位置索引。
                    index = fallback_index
                # 变量说明：current 表示当前步骤使用的 current 值。
                current = tool_calls.setdefault(index, {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                # 变量说明：call_id 表示call 对象的唯一标识。
                call_id = raw_call.get("id")
                if call_id:
                    current["id"] = str(call_id)
                # 变量说明：call_type 表示当前步骤使用的 call_type 值。
                call_type = raw_call.get("type")
                if call_type:
                    current["type"] = str(call_type)
                # 变量说明：function 表示当前步骤使用的 function 值。
                function = raw_call.get("function") or {}
                if not isinstance(function, Mapping):
                    # 变量说明：function 表示当前步骤使用的 function 值。
                    function = _as_mapping(function)
                # 变量说明：name 表示当前对象名称。
                name = function.get("name")
                if name:
                    current["function"]["name"] += str(name)
                # 变量说明：arguments 表示当前流程使用的 arguments 集合。
                arguments = function.get("arguments")
                if arguments:
                    current["function"]["arguments"] += str(arguments)
            # 变量说明：chunk_usage 表示当前步骤使用的 chunk_usage 值。
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
    # 变量说明：assembled_calls 表示当前流程使用的 assembled_calls 集合。
    assembled_calls: list[dict[str, Any]] = []
    for index, item in sorted(tool_calls.items()):
        assembled_calls.append({
            "id": item["id"] or f"call-{index + 1}",
            "type": item["type"],
            "function": dict(item["function"]),
        })
    # 变量说明：payload 表示跨层传递的数据载荷。
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



# 函数职责：异步完成 create 对应的业务处理。
# 参数关系：config 表示当前生效的配置；api_key 表示当前步骤使用的 api_key 值；messages 表示发送给模型或客户端的消息序列；tools 表示本轮可调用的工具集合；mode 表示当前步骤使用的 mode 值；prompt_cache_key 表示当前步骤使用的 prompt_cache_key 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def create(config: ProviderConfig, api_key: str, messages: list[dict], tools: list[dict], mode: str, prompt_cache_key: str | None):
    # 变量说明：model 表示当前选择的模型。
    model = _litellm_model(config.provider, config.model_id)
    # 变量说明：kwargs 表示当前流程使用的 kwargs 集合。
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
        # 变量说明：kwargs 的索引项 表示该语句创建或更新的目标数据。
        kwargs["prompt_cache_key"] = prompt_cache_key
    if config.custom_headers:
        # 变量说明：kwargs 的索引项 表示该语句创建或更新的目标数据。
        kwargs["extra_headers"] = dict(config.custom_headers)
    if mode != "compaction" and config.thinking_level not in {"off", "auto", ""}:
        # 变量说明：kwargs 的索引项 表示该语句创建或更新的目标数据。
        kwargs["reasoning_effort"] = config.thinking_level

    try:
        # 变量说明：response 表示下游返回的响应。
        response = await litellm.acompletion(**kwargs)
    except BaseException as exc:
        if not _streaming_unsupported(exc):
            raise
        # 变量说明：fallback_kwargs 表示当前流程使用的 fallback_kwargs 集合。
        fallback_kwargs = {key: value for key, value in kwargs.items() if key != "stream_options"}
        fallback_kwargs["stream"] = False
        # 变量说明：response 表示下游返回的响应。
        response = await litellm.acompletion(**fallback_kwargs)

    return response
