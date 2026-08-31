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
            result.extend(dict(item) for item in native["items"])
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


def function_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", **tool["function"], "strict": tool["function"].get("strict", False)} for tool in tools]


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
        else:
            raise IncompleteResponse(f"模型返回了未授权的输出项类型：{kind}")
    return {"content": "".join(text), "reasoning_content": "".join(summaries),
            "tool_calls": calls, "usage": dict(usage or {}),
            "_pgagent_provider": {"protocol": "responses", "items": items}}


async def consume(stream: Any, *, idle_seconds: float, on_delta=None, on_thought_delta=None, on_activity=None) -> dict[str, Any]:
    completed: list[dict[str, Any]] = []
    finished: dict[str, Any] | None = None
    try:
        async for raw in stream_events(stream, idle_seconds):
            event = _as_mapping(raw)
            await _emit_activity(on_activity)
            kind = event.get("type")
            if kind == "response.output_text.delta":
                await _emit_delta(on_delta, str(event.get("delta") or ""))
            elif kind == "response.reasoning_summary_text.delta":
                await _emit_delta(on_thought_delta, str(event.get("delta") or ""))
            elif kind == "response.output_item.done":
                item = _as_mapping(event["item"])
                if item.get("status") not in {"incomplete", "in_progress"}:
                    completed.append(item)
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
    return project_items(list(finished.get("output") or completed), finished.get("usage"))
