"""Native Responses items and events, without a Chat Completions conversion hop."""
# 文件职责：负责模型连接、请求、协议转换与流式响应中的 responses 子模块。
# 逻辑关系：上层通过 model/protocols/responses.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from src.model.protocols.common import _as_mapping, _emit_activity, _emit_delta
from src.model.output import (
    AssistantMessageItem,
    EndTurn,
    HostedToolItem,
    LocalToolCallItem,
    NormalizedModelResponse,
    OutputPhase,
    ReasoningItem,
    ResponseStatus,
)
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
_INTERNAL_OUTPUT_INDEX = "_pgagent_output_index"


def _provider_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Remove adapter-only correlation fields before provider/history use."""
    return {key: value for key, value in item.items() if key != _INTERNAL_OUTPUT_INDEX}

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
            "_pgagent_provider": {
                "protocol": "responses",
                "items": [_provider_item(item) for item in items],
            }}


def _item_key(item: Mapping[str, Any], fallback_index: int) -> tuple[str, Any]:
    """Use provider identity first; index keeps anonymous SDK items stable."""
    return (
        ("id", item["id"])
        if item.get("id")
        else ("index", item.get("output_index", item.get(_INTERNAL_OUTPUT_INDEX, fallback_index)))
    )


def _merge_items(done_items: list[dict[str, Any]], final_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge output_item.done with response.completed without replaying side effects."""
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    # completed output is authoritative when the provider repeats an item in
    # response.completed; final-only items are appended in provider order.
    for index, item in enumerate(final_items):
        current = dict(item)
        key = _item_key(current, index)
        if key in seen:
            label = "item id" if key[0] == "id" else "output_index"
            raise ValueError(f"duplicate {label}: {key[1]}")
        seen.add(key)
        merged.append(current)
    for index, item in enumerate(done_items):
        current = dict(item)
        key = _item_key(current, index)
        if key not in seen:
            seen.add(key)
            merged.append(current)
    return merged


def merge_replayed_items(
    prior_items: list[dict[str, Any]], current_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep replay history first while suppressing a provider repeat once."""
    merged = [dict(item) for item in prior_items]
    seen = {_item_key(item, index) for index, item in enumerate(merged)}
    current_seen: set[tuple[str, Any]] = set()
    for index, item in enumerate(current_items):
        current = dict(item)
        key = _item_key(current, len(merged) + index)
        if key in current_seen:
            label = "item id" if key[0] == "id" else "output_index"
            raise ValueError(f"duplicate {label}: {key[1]}")
        current_seen.add(key)
        if key not in seen:
            seen.add(key)
            merged.append(current)
    return merged


def _normalize_item(item: Mapping[str, Any], response_id: str | None, output_index: int) -> Any:
    kind = str(item.get("type") or "")
    public_item = _provider_item(item)
    common = {
        "response_id": response_id,
        "item_id": str(item["id"]) if item.get("id") else None,
        "output_index": item.get("output_index", item.get(_INTERNAL_OUTPUT_INDEX, output_index)),
    }
    if kind == "message":
        text = []
        for part in item.get("content") or []:
            part = _as_mapping(part)
            if part.get("type") == "output_text":
                text.append(str(part.get("text") or ""))
            elif part.get("type") == "refusal":
                text.append(str(part.get("refusal") or ""))
        return AssistantMessageItem(
            **common, content="".join(text),
            phase=item.get("phase", OutputPhase.UNKNOWN),
            end_turn=item.get("end_turn", EndTurn.UNKNOWN),
        )
    if kind == "reasoning":
        return ReasoningItem(
            **common, summary=item.get("summary"),
            encrypted_content=item.get("encrypted_content"),
            provider_data=public_item,
        )
    if kind == "function_call":
        arguments: Any = item.get("arguments", "")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass
        return LocalToolCallItem(
            **common, call_id=str(item.get("call_id") or item.get("id") or "call"),
            tool_name=str(item.get("name") or ""), arguments=arguments,
        )
    if kind in _HOSTED_OUTPUT_ITEMS:
        return HostedToolItem(
            **common, tool_name=kind.removesuffix("_call"),
            status=str(item.get("status") or "completed"),
            call_id=str(item["call_id"]) if item.get("call_id") else None,
            details=public_item,
        )
    raise IncompleteResponse(f"模型返回了未授权的输出项类型：{kind}")


def normalize_response(response: Mapping[str, Any]) -> NormalizedModelResponse:
    """Convert a completed native Responses object into the shared model."""
    raw = dict(response)
    response_id = str(raw["id"]) if raw.get("id") else None
    raw_items = [dict(item) for item in raw.get("output") or []]
    normalized_items = [_normalize_item(item, response_id, index) for index, item in enumerate(raw_items)]
    items = [_provider_item(item) for item in raw_items]
    status = ResponseStatus(str(raw.get("status") or "unknown"))
    return NormalizedModelResponse(
        response_id=response_id, items=normalized_items, status=status,
        provider_payload={"protocol": "responses", "items": items},
        usage=dict(raw.get("usage") or {}),
    )


def to_legacy_payload(response: NormalizedModelResponse) -> dict[str, Any]:
    native = response.provider_payload if isinstance(response.provider_payload, Mapping) else {}
    items = [dict(item) for item in native.get("items") or []]
    return project_items(items, response.usage)


# 函数职责：异步完成 consume 对应的业务处理。
# 参数关系：stream 表示当前步骤使用的 stream 值；idle_seconds 表示当前流程使用的 idle_seconds 集合；on_delta 表示当前步骤使用的 on_delta 值；on_thought_delta 表示当前步骤使用的 on_thought_delta 值；on_activity 表示当前步骤使用的 on_activity 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def consume(stream: Any, *, idle_seconds: float, on_delta=None, on_thought_delta=None,
                  on_activity=None, on_assistant_item=None, on_assistant_item_completed=None,
                  on_item=None) -> NormalizedModelResponse:
    # 变量说明：completed 表示当前步骤使用的 completed 值。
    completed: list[dict[str, Any]] = []
    completed_keys: set[tuple[str, Any]] = set()
    completed_call_ids: set[str] = set()
    # 变量说明：finished 表示当前步骤使用的 finished 值。
    finished: dict[str, Any] | None = None
    # 变量说明：summary_lengths 表示当前流程使用的 summary_lengths 集合。
    summary_lengths: dict[tuple[str, int], int] = {}
    response_id: str | None = None
    assistant_contents: dict[str, str] = {}
    assistant_metadata: dict[str, dict[str, Any]] = {}
    assistant_emitted_content: dict[str, str] = {}
    assistant_completed_keys: set[str] = set()
    assistant_callback = on_assistant_item or on_item
    assistant_completed_callback = on_assistant_item_completed

    def assistant_key(item_id: str | None, output_index: int | None) -> str:
        return item_id or f"index:{output_index}"

    async def emit_assistant(item: AssistantMessageItem) -> None:
        if assistant_callback is None:
            return
        key = assistant_key(item.item_id, item.output_index)
        if assistant_emitted_content.get(key) == item.content:
            return
        assistant_emitted_content[key] = item.content
        await _emit_assistant_item(assistant_callback, item)

    async def complete_assistant(item: AssistantMessageItem) -> None:
        if assistant_completed_callback is None:
            return
        key = assistant_key(item.item_id, item.output_index)
        if key in assistant_completed_keys:
            return
        assistant_completed_keys.add(key)
        await _emit_assistant_item(assistant_completed_callback, item)

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
            if event.get("response_id"):
                response_id = str(event["response_id"])
            if kind == "response.output_item.added":
                item = _as_mapping(event["item"])
                if item.get("type") == "message":
                    output_index = event.get("output_index")
                    if not isinstance(output_index, int) or isinstance(output_index, bool):
                        output_index = item.get("output_index")
                    item_id = str(item["id"]) if item.get("id") else None
                    key = assistant_key(item_id, output_index)
                    assistant_metadata[key] = item
                    await emit_assistant(AssistantMessageItem(
                        response_id=response_id,
                        item_id=item_id,
                        output_index=output_index,
                        content=assistant_contents.get(item_id or key, ""),
                        phase=item.get("phase", OutputPhase.UNKNOWN),
                        end_turn=item.get("end_turn", EndTurn.UNKNOWN),
                    ))
            elif kind == "response.output_text.delta":
                delta = str(event.get("delta") or "")
                await _emit_delta(on_delta, delta)
                item_id = str(event.get("item_id") or "message-0")
                assistant_contents[item_id] = assistant_contents.get(item_id, "") + delta
                if assistant_callback is not None:
                    output_index = (
                        event.get("output_index")
                        if isinstance(event.get("output_index"), int)
                        and not isinstance(event.get("output_index"), bool)
                        else None
                    )
                    metadata = assistant_metadata.get(assistant_key(item_id, output_index), {})
                    await emit_assistant(AssistantMessageItem(
                        response_id=response_id, item_id=item_id,
                        output_index=output_index,
                        content=assistant_contents[item_id],
                        phase=metadata.get("phase", OutputPhase.UNKNOWN),
                        end_turn=metadata.get("end_turn", EndTurn.UNKNOWN),
                    ))
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
                if (
                    not item.get("id")
                    and item.get("output_index") is None
                    and event.get("output_index") is not None
                ):
                    # SDK 把 output_index 放在 done event 上；内部保留它以便
                    # 多次断流合并，发送给 provider 前由 _provider_item 剥离。
                    item[_INTERNAL_OUTPUT_INDEX] = event["output_index"]
                if item.get("status") not in {"incomplete", "in_progress"}:
                    key = (
                        ("id", item["id"])
                        if item.get("id")
                        else ("index", event.get("output_index", len(completed)))
                    )
                    if key in completed_keys:
                        continue
                    call_id = str(item.get("call_id") or "").strip()
                    if call_id and call_id in completed_call_ids:
                        raise ValueError(f"duplicate call id: {call_id}")
                    completed_keys.add(key)
                    if call_id:
                        completed_call_ids.add(call_id)
                    completed.append(item)
                    await emit_item_summary(item)
                    if item.get("type") == "message":
                        normalized_item = _normalize_item(item, response_id, len(completed) - 1)
                        await emit_assistant(normalized_item)
                        await complete_assistant(normalized_item)
            elif kind == "response.completed":
                # 变量说明：finished 表示当前步骤使用的 finished 值。
                finished = _as_mapping(event["response"])
                response_id = str(finished["id"]) if finished.get("id") else response_id
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
    except ValueError:
        # 重复 call id 是确定性协议错误，不能伪装成可重试断流。
        raise
    except StreamInterrupted as exc:
        if finished is not None:
            # response.completed is the provider boundary; a timeout while
            # draining an optional tail must not discard the completed reply.
            merged_items = _merge_items(
                completed, [dict(item) for item in finished.get("output") or []]
            )
            return NormalizedModelResponse(
                response_id=response_id,
                items=[_normalize_item(item, response_id, index) for index, item in enumerate(merged_items)],
                status=ResponseStatus(str(finished.get("status") or "completed")),
                provider_payload={
                    "protocol": "responses",
                    "items": [_provider_item(item) for item in merged_items],
                },
                usage=dict(finished.get("usage") or {}),
            )
        exc.completed_items = list(completed)
        raise
    except IncompleteResponse:
        raise
    except Exception as exc:
        raise StreamInterrupted("模型响应流中断", completed_items=completed) from exc
    if finished is None:
        raise StreamInterrupted("模型流在 response.completed 之前关闭", completed_items=completed)
    # 变量说明：items 表示待处理的元素集合。
    items = _merge_items(completed, [dict(item) for item in finished.get("output") or []])
    for item in items:
        await emit_item_summary(item)
    for index, item in enumerate(items):
        if item.get("type") == "message":
            assistant_item = _normalize_item(item, response_id, index)
            await emit_assistant(assistant_item)
            await complete_assistant(assistant_item)
    normalized = NormalizedModelResponse(
        response_id=response_id,
        items=[_normalize_item(item, response_id, index) for index, item in enumerate(items)],
        status=ResponseStatus(str(finished.get("status") or "completed")),
        provider_payload={
            "protocol": "responses",
            "items": [_provider_item(item) for item in items],
        },
        usage=dict(finished.get("usage") or {}),
    )
    return normalized


async def _emit_assistant_item(callback: Any, item: AssistantMessageItem) -> None:
    result = callback(item)
    if asyncio.iscoroutine(result):
        await result
