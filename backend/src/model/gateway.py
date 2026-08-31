"""Model routing and session-private attachment hydration."""
from __future__ import annotations

import json
from typing import Any, Mapping

import litellm
from src.model.config import ProviderConfig
from src.model.credentials import get_api_key
from src.model.protocols.common import ModelConfigurationError, PartialModelStreamError, _litellm_model

def _hydrate_attachment_messages(
    messages: list[dict[str, Any]],
    attachment_store: Any | None,
) -> list[dict[str, Any]]:
    hydrated: list[dict[str, Any]] = []
    pending_tool_images: list[Mapping[str, Any]] = []
    for index, message in enumerate(messages):
        provider_message = {
            key: value for key, value in message.items()
            if key == "_pgagent_provider" or not str(key).startswith("_pgagent_")
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

    call_with_attachments.manages_retries = getattr(model_call, "manages_retries", False)
    return call_with_attachments



def build_model_call(config: ProviderConfig):
    from src.model.request import create_model_call
    return create_model_call(config, credentials=get_api_key)
