"""Native Responses items and events, without a Chat Completions conversion hop."""
# 文件职责：负责模型连接、请求、协议转换与流式响应中的 responses 子模块。
# 逻辑关系：上层通过 model/protocols/responses.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from src.model.protocols.common import _as_mapping, _emit_activity, _emit_delta
from src.model.streaming import IncompleteResponse, StreamInterrupted, stream_events


# 函数职责：完成 input_items 对应的业务处理。
# 参数关系：messages 表示发送给模型或客户端的消息序列。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def input_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 变量说明：result 表示本步骤产生的结果。
    result: list[dict[str, Any]] = []
    for message in messages:
        # 变量说明：native 表示当前步骤使用的 native 值。
        native = message.get("_pgagent_provider") or {}
        if native.get("protocol") == "responses" and native.get("items"):
            for item in native["items"]:
                # 变量说明：replay_item 表示当前步骤使用的 replay_item 值。
                replay_item = dict(item)
                # reasoning 的输出状态不能回传给当前上游；也处理已保存的旧历史。
                if replay_item.get("type") == "reasoning":
                    replay_item.pop("status", None)
                result.append(replay_item)
            continue
        # 变量说明：role 表示当前步骤使用的 role 值。
        role = message.get("role", "user")
        # 变量说明：content 表示待处理或返回的正文内容。
        content = message.get("content") or ""
        if role == "tool":
            result.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                           "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
            continue
        if content:
            if isinstance(content, list):
                # 变量说明：parts 表示当前流程使用的 parts 集合。
                parts = []
                for part in content:
                    if part.get("type") == "image_url":
                        # 变量说明：image 表示当前步骤使用的 image 值。
                        image = part["image_url"]
                        parts.append({"type": "input_image", "image_url": image["url"],
                                      "detail": image.get("detail", "auto")})
                    elif part.get("type") == "text":
                        parts.append({"type": "input_text", "text": part["text"]})
                    else:
                        raise ValueError(f"Responses 不支持此输入内容类型：{part.get('type')}")
                # 变量说明：content 表示待处理或返回的正文内容。
                content = parts
            result.append({"role": role, "content": content})
        for call in message.get("tool_calls") or []:
            # 变量说明：function 表示当前步骤使用的 function 值。
            function = call["function"]
            # 变量说明：arguments 表示当前流程使用的 arguments 集合。
            arguments = function["arguments"]
            result.append({"type": "function_call", "call_id": call["id"], "name": function["name"],
                           "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)})
    return result


# 函数职责：完成 response_tools 对应的业务处理。
# 参数关系：tools 表示本轮可调用的工具集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def response_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project the shared tool catalog into native Responses tool definitions."""
    # 变量说明：result 表示本步骤产生的结果。
    result: list[dict[str, Any]] = []
    for tool in tools:
        # 变量说明：kind 表示当前步骤使用的 kind 值。
        kind = tool.get("type")
        if kind == "function" and isinstance(tool.get("function"), Mapping):
            # 变量说明：function 表示当前步骤使用的 function 值。
            function = dict(tool["function"])
            if function.get("name") == "web_search":
                # The registry stays protocol-neutral: Chat Completions uses
                # the local fallback, while Responses receives its hosted tool.
                result.append({"type": "web_search"})
                continue
            # 变量说明：function 的索引项 表示该语句创建或更新的目标数据。
            function["strict"] = function.get("strict", False)
            result.append({"type": "function", **function})
        else:
            # Hosted tools such as web_search are executed by the Responses
            # service and already use the API's native shape.
            result.append(dict(tool))
    return result


# Output items executed by the provider.  They remain in native history but do
# not become PGAgent function calls for the local dispatcher.
# 变量说明：_HOSTED_OUTPUT_ITEMS 表示当前流程使用的 _HOSTED_OUTPUT_ITEMS 集合。
_HOSTED_OUTPUT_ITEMS = {
    "web_search_call",
    "file_search_call",
    "code_interpreter_call",
    "image_generation_call",
}

# Responses 可能在已建立的事件流内报告临时服务故障；这些错误没有 HTTP
# 状态码可供外层重试器判断，因此只对明确的服务过载类型开放流式重试。
_RETRYABLE_RESPONSE_ERROR_CODES = {
    "rate_limit_exceeded",
    "server_error",
    "server_is_overloaded",
    "vector_store_timeout",
}
_RETRYABLE_RESPONSE_ERROR_TYPES = {
    "rate_limit_error",
    "server_error",
    "service_unavailable_error",
}


def response_failure_error(
    response: Mapping[str, Any],
    event_type: str,
) -> IncompleteResponse | StreamInterrupted:
    """Build a sanitized typed failure while preserving stable provider codes."""

    nested = response.get("error") or response.get("incomplete_details")
    error = _as_mapping(nested) if nested else dict(response) if event_type == "error" else {}
    error_code = str(error.get("code") or error.get("reason") or "").strip()
    error_type = str(error.get("type") or "").strip()
    reason = error_code or error_type or str(event_type)
    if (
        event_type in {"response.failed", "error"}
        and (
            error_code in _RETRYABLE_RESPONSE_ERROR_CODES
            or error_type in _RETRYABLE_RESPONSE_ERROR_TYPES
        )
    ):
        return StreamInterrupted(
            f"模型服务暂时不可用：{reason}",
            provider_error_code=error_code,
            provider_error_type=error_type,
        )
    return IncompleteResponse(
        f"模型响应未完成：{reason}",
        provider_error_code=error_code,
        provider_error_type=error_type,
    )


# 函数职责：完成 project_items 对应的业务处理。
# 参数关系：items 表示待处理的元素集合；usage 表示当前步骤使用的 usage 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def project_items(items: list[dict[str, Any]], usage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    # 变量说明：text 表示当前步骤使用的 text 值。
    text: list[str] = []
    # 变量说明：summaries 表示当前流程使用的 summaries 集合。
    summaries: list[str] = []
    # 变量说明：calls 表示当前流程使用的 calls 集合。
    calls: list[dict[str, Any]] = []
    for item in items:
        # 变量说明：kind 表示当前步骤使用的 kind 值。
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    text.append(part.get("refusal", ""))
        elif kind == "reasoning":
            summaries.extend(part.get("text", "") for part in item.get("summary") or [])
        elif kind == "function_call":
            calls.append({"id": item["call_id"], "type": "function", "function": {
                "name": item["name"], "arguments": item["arguments"],
            }})
        elif kind in _HOSTED_OUTPUT_ITEMS:
            continue
        else:
            raise IncompleteResponse(f"模型返回了未授权的输出项类型：{kind}")
    return {"content": "".join(text), "reasoning_content": "".join(summaries),
            "tool_calls": calls, "usage": dict(usage or {}),
            "_pgagent_provider": {"protocol": "responses", "items": items}}


# 函数职责：异步完成 consume 对应的业务处理。
# 参数关系：stream 表示当前步骤使用的 stream 值；idle_seconds 表示当前流程使用的 idle_seconds 集合；on_delta 表示当前步骤使用的 on_delta 值；on_thought_delta 表示当前步骤使用的 on_thought_delta 值；on_activity 表示当前步骤使用的 on_activity 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def consume(stream: Any, *, idle_seconds: float, on_delta=None, on_thought_delta=None, on_activity=None) -> dict[str, Any]:
    # 变量说明：completed 表示当前步骤使用的 completed 值。
    completed: list[dict[str, Any]] = []
    # 变量说明：finished 表示当前步骤使用的 finished 值。
    finished: dict[str, Any] | None = None
    # 变量说明：summary_lengths 表示当前流程使用的 summary_lengths 集合。
    summary_lengths: dict[tuple[str, int], int] = {}

    # 函数职责：异步发送 summary 对应的数据或流程。
    # 参数关系：item_id 表示item 对象的唯一标识；index 表示当前元素的位置索引；text 表示当前步骤使用的 text 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def emit_summary(item_id: str, index: int, text: str) -> None:
        # done 和最终 output 会重带完整摘要，只补发该段尚未展示的尾部。
        # 变量说明：key 表示用于查找或映射的键。
        key = (item_id, index)
        # 变量说明：offset 表示当前步骤使用的 offset 值。
        offset = summary_lengths.get(key, 0)
        await _emit_delta(on_thought_delta, text[offset:])
        summary_lengths[key] = max(offset, len(text))

    # 函数职责：异步发送 item_summary 对应的数据或流程。
    # 参数关系：item 表示当前步骤使用的 item 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def emit_item_summary(item: dict[str, Any]) -> None:
        if item.get("type") == "reasoning":
            for index, part in enumerate(item.get("summary") or []):
                await emit_summary(item.get("id", ""), index, part.get("text", ""))

    try:
        async for raw in stream_events(stream, idle_seconds):
            # 保留服务端实际返回的字段，不将 SDK 默认值补进下一轮历史。
            # 变量说明：event 表示当前运行事件。
            event = _as_mapping(raw, exclude_unset=True)
            await _emit_activity(on_activity)
            # 变量说明：kind 表示当前步骤使用的 kind 值。
            kind = event.get("type")
            if kind == "response.output_text.delta":
                await _emit_delta(on_delta, str(event.get("delta") or ""))
            elif kind == "response.reasoning_summary_text.delta":
                # 变量说明：delta 表示当前步骤使用的 delta 值。
                delta = str(event.get("delta") or "")
                # 变量说明：key 表示用于查找或映射的键。
                key = (event.get("item_id", ""), event.get("summary_index", 0))
                await _emit_delta(on_thought_delta, delta)
                summary_lengths[key] = summary_lengths.get(key, 0) + len(delta)
            elif kind == "response.reasoning_summary_text.done":
                await emit_summary(event.get("item_id", ""), event.get("summary_index", 0),
                                   str(event.get("text") or ""))
            elif kind == "response.output_item.done":
                # 变量说明：item 表示当前步骤使用的 item 值。
                item = _as_mapping(event["item"])
                if item.get("status") not in {"incomplete", "in_progress"}:
                    completed.append(item)
                    await emit_item_summary(item)
            elif kind == "response.completed":
                # 变量说明：finished 表示当前步骤使用的 finished 值。
                finished = _as_mapping(event["response"])
                break
            elif kind in {"response.failed", "response.incomplete", "error"}:
                # 变量说明：response 表示下游返回的响应。
                response = _as_mapping(event.get("response") or event)
                failure = response_failure_error(response, str(kind))
                if isinstance(failure, StreamInterrupted):
                    failure.completed_items = list(completed)
                raise failure
    except asyncio.CancelledError:
        raise
    except StreamInterrupted:
        raise
    except IncompleteResponse:
        raise
    except Exception as exc:
        raise StreamInterrupted("模型响应流中断", completed_items=completed) from exc
    if finished is None:
        raise StreamInterrupted("模型流在 response.completed 之前关闭", completed_items=completed)
    # 变量说明：items 表示待处理的元素集合。
    items = list(finished.get("output") or completed)
    for item in items:
        await emit_item_summary(item)
    return project_items(items, finished.get("usage"))
