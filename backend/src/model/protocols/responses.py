"""Native Responses items and events, without a Chat Completions conversion hop."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from src.model.protocols.common import _as_mapping, _emit_activity, _emit_delta
from src.model.streaming import IncompleteResponse, StreamInterrupted, stream_events


def input_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        native = message.get("_pgagent_provider") or {}
        if native.get("protocol") == "responses" and native.get("items"):
            for item in native["items"]:
                replay_item = dict(item)
                # reasoning 的输出状态不能回传给当前上游；也处理已保存的旧历史。
                if replay_item.get("type") == "reasoning":
                    replay_item.pop("status", None)
                result.append(replay_item)
            continue
        role = message.get("role", "user")
        content = message.get("content") or ""
        if role == "tool":
            result.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                           "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
            continue
        if content:
            if isinstance(content, list):
                parts = []
                for part in content:
                    if part.get("type") == "image_url":
                        image = part["image_url"]
                        parts.append({"type": "input_image", "image_url": image["url"],
                                      "detail": image.get("detail", "auto")})
                    elif part.get("type") == "text":
                        parts.append({"type": "input_text", "text": part["text"]})
                    else:
                        raise ValueError(f"Responses 不支持此输入内容类型：{part.get('type')}")
                content = parts
            result.append({"role": role, "content": content})
        for call in message.get("tool_calls") or []:
            function = call["function"]
            arguments = function["arguments"]
            result.append({"type": "function_call", "call_id": call["id"], "name": function["name"],
                           "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)})
    return result


def response_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project the shared tool catalog into native Responses tool definitions."""
    result: list[dict[str, Any]] = []
    for tool in tools:
        kind = tool.get("type")
        if kind == "function" and isinstance(tool.get("function"), Mapping):
            function = dict(tool["function"])
            if function.get("name") == "web_search":
                # The registry stays protocol-neutral: Chat Completions uses
                # the local fallback, while Responses receives its hosted tool.
                result.append({"type": "web_search"})
                continue
            function["strict"] = function.get("strict", False)
            result.append({"type": "function", **function})
        else:
            # Hosted tools such as web_search are executed by the Responses
            # service and already use the API's native shape.
            result.append(dict(tool))
    return result


# Output items executed by the provider.  They remain in native history but do
# not become PGAgent function calls for the local dispatcher.
_HOSTED_OUTPUT_ITEMS = {
    "web_search_call",
    "file_search_call",
    "code_interpreter_call",
    "image_generation_call",
}


def project_items(items: list[dict[str, Any]], usage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    text: list[str] = []
    summaries: list[str] = []
    calls: list[dict[str, Any]] = []
    for item in items:
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


async def consume(stream: Any, *, idle_seconds: float, on_delta=None, on_thought_delta=None, on_activity=None) -> dict[str, Any]:
    completed: list[dict[str, Any]] = []
    finished: dict[str, Any] | None = None
    summary_lengths: dict[tuple[str, int], int] = {}

    async def emit_summary(item_id: str, index: int, text: str) -> None:
        # done 和最终 output 会重带完整摘要，只补发该段尚未展示的尾部。
        key = (item_id, index)
        offset = summary_lengths.get(key, 0)
        await _emit_delta(on_thought_delta, text[offset:])
        summary_lengths[key] = max(offset, len(text))

    async def emit_item_summary(item: dict[str, Any]) -> None:
        if item.get("type") == "reasoning":
            for index, part in enumerate(item.get("summary") or []):
                await emit_summary(item.get("id", ""), index, part.get("text", ""))

    try:
        async for raw in stream_events(stream, idle_seconds):
            # 保留服务端实际返回的字段，不将 SDK 默认值补进下一轮历史。
            event = _as_mapping(raw, exclude_unset=True)
            await _emit_activity(on_activity)
            kind = event.get("type")
            if kind == "response.output_text.delta":
                await _emit_delta(on_delta, str(event.get("delta") or ""))
            elif kind == "response.reasoning_summary_text.delta":
                delta = str(event.get("delta") or "")
                key = (event.get("item_id", ""), event.get("summary_index", 0))
                await _emit_delta(on_thought_delta, delta)
                summary_lengths[key] = summary_lengths.get(key, 0) + len(delta)
            elif kind == "response.reasoning_summary_text.done":
                await emit_summary(event.get("item_id", ""), event.get("summary_index", 0),
                                   str(event.get("text") or ""))
            elif kind == "response.output_item.done":
                item = _as_mapping(event["item"])
                if item.get("status") not in {"incomplete", "in_progress"}:
                    completed.append(item)
                    await emit_item_summary(item)
            elif kind == "response.completed":
                finished = _as_mapping(event["response"])
                break
            elif kind in {"response.failed", "response.incomplete", "error"}:
                response = _as_mapping(event.get("response") or event)
                error = response.get("error") or response.get("incomplete_details") or {}
                raise IncompleteResponse(f"模型响应未完成：{error.get('code') or error.get('reason') or kind}")
    except asyncio.CancelledError:
        raise
    except IncompleteResponse:
        raise
    except Exception as exc:
        raise StreamInterrupted("模型响应流中断", completed_items=completed) from exc
    if finished is None:
        raise StreamInterrupted("模型流在 response.completed 之前关闭", completed_items=completed)
    items = list(finished.get("output") or completed)
    for item in items:
        await emit_item_summary(item)
    return project_items(items, finished.get("usage"))
