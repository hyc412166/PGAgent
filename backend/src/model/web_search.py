"""模型服务端搜索的能力识别与实际连接检测。"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx


# 官方文档明确支持的模型；未知模型仍由连接检测提供证据。
_OPENAI_WEB_SEARCH_MODELS = {"gpt-4.1", "gpt-4.1-mini", "gpt-5.4", "gpt-5.5", "gpt-6-astra"}


def supports_hosted_web_search(config: Any) -> bool:
    if config.api_protocol != "responses":
        return False
    if config.model_id == "gpt-5" and config.thinking_level == "minimal":
        return False
    evidence = config.capabilities.get("hosted_web_search", {})
    # 证据只属于实际检测的地址和模型，不能跨中转地址复用。
    if evidence.get("base_url") == config.base_url.strip().rstrip("/"):
        result = evidence.get("models", {}).get(config.model_id)
        if result is not None:
            return result.get("supported") is True
    origin = urlsplit(config.base_url)
    return (
        origin.scheme == "https" and origin.hostname == "api.openai.com"
        and origin.port in {None, 443} and origin.path.rstrip("/") == "/v1"
        and config.model_id in _OPENAI_WEB_SEARCH_MODELS
    )


def probe_hosted_web_search(base_url: str, api_key: str, model_id: str,
                           custom_headers: dict[str, str] | None = None) -> dict[str, Any]:
    """必须收到完成的原生搜索项；接受 tools 参数或普通对话成功都不算支持。"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(custom_headers or {})
    payload = {
        "model": model_id, "input": "Search for the official OpenAI website. Return its URL only.",
        "tools": [{"type": "web_search"}], "tool_choice": {"type": "web_search"},
        "max_output_tokens": 512, "store": False, "stream": True,
    }
    completed = False
    search_completed = False
    try:
        with httpx.Client(timeout=httpx.Timeout(60, connect=12)) as client:
            with client.stream("POST", f"{base_url.rstrip('/')}/responses", headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    unsupported = response.status_code in {400, 404, 422}
                    return {"supported": False if unsupported else None,
                            "category": "unsupported" if unsupported else "provider_error",
                            "message": f"服务端搜索检测返回 HTTP {response.status_code}"}
                for line in response.iter_lines():
                    if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                        continue
                    event = json.loads(line[5:].strip())
                    if not isinstance(event, Mapping):
                        return {"supported": None, "category": "invalid_response", "message": "服务端搜索检测返回非对象事件"}
                    kind = event.get("type")
                    if kind in {"response.failed", "response.incomplete", "error"}:
                        # 部分中转把 error 返回为字符串；这是检测失败，不能让连接 API 变成 500。
                        failed_response = event.get("response") or event
                        error = failed_response.get("error") if isinstance(failed_response, Mapping) else None
                        error_code = error.get("code") if isinstance(error, Mapping) else None
                        return {"supported": None, "category": "provider_error",
                                "message": "服务端搜索检测未完成", "error_code": str(error_code or kind)[:160]}
                    items = [event.get("item") or {}] if kind == "response.output_item.done" else []
                    if kind == "response.completed":
                        response_data = event.get("response") or {}
                        if not isinstance(response_data, Mapping) or not isinstance(response_data.get("output", []), list):
                            return {"supported": None, "category": "invalid_response", "message": "服务端搜索检测返回无效结果"}
                        completed = True
                        items.extend(response_data.get("output") or [])
                    if any(not isinstance(item, Mapping) for item in items):
                        return {"supported": None, "category": "invalid_response", "message": "服务端搜索检测返回无效输出项"}
                    search_completed = search_completed or any(
                        item.get("type") == "web_search_call" and item.get("status") == "completed"
                        for item in items
                    )
    except (httpx.HTTPError, ValueError) as exc:
        return {"supported": None, "category": "network_error" if isinstance(exc, httpx.HTTPError) else "invalid_response",
                "message": f"服务端搜索检测失败：{type(exc).__name__}"}
    supported = completed and search_completed
    return {"supported": supported, "category": "ok" if supported else "unsupported",
            "message": "已验证服务端搜索" if supported else "接口未返回完成的原生搜索调用"}
