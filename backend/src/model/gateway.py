"""Model routing and session-private attachment hydration."""
# 文件职责：根据连接配置选择模型协议适配器，发送统一请求并把供应商响应转换为内部模型事件。
# 逻辑关系：AgentRuntime 提交标准化消息和工具定义；网关委派 protocols.responses 或 chat_completions 发起请求，再将文本、工具调用和用量交还运行引擎。
from __future__ import annotations

import json
from typing import Any, Mapping

import litellm
from src.model.config import ProviderConfig
from src.model.credentials import get_api_key
from src.model.protocols.common import ModelConfigurationError, PartialModelStreamError, _litellm_model

# 函数职责：完成 hydrate_attachment_messages 对应的业务处理。
# 参数关系：messages 表示发送给模型或客户端的消息序列；attachment_store 表示当前步骤使用的 attachment_store 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _hydrate_attachment_messages(
    messages: list[dict[str, Any]],
    attachment_store: Any | None,
) -> list[dict[str, Any]]:
    # 变量说明：hydrated 表示当前步骤使用的 hydrated 值。
    hydrated: list[dict[str, Any]] = []
    # 变量说明：pending_tool_images 表示当前流程使用的 pending_tool_images 集合。
    pending_tool_images: list[Mapping[str, Any]] = []
    for index, message in enumerate(messages):
        # 变量说明：provider_message 表示当前步骤使用的 provider_message 值。
        provider_message = {
            key: value for key, value in message.items()
            if key == "_pgagent_provider" or not str(key).startswith("_pgagent_")
        }
        # 变量说明：content 表示待处理或返回的正文内容。
        content = message.get("content")
        if not isinstance(content, list):
            hydrated.append(provider_message)
        else:
            # 变量说明：parts 表示当前流程使用的 parts 集合。
            parts: list[dict[str, Any]] = []
            for raw_part in content:
                if not isinstance(raw_part, Mapping) or raw_part.get("type") != "pgagent_image_ref":
                    parts.append(dict(raw_part) if isinstance(raw_part, Mapping) else {"type": "text", "text": str(raw_part)})
                    continue
                # 变量说明：attachment_id 表示attachment 对象的唯一标识。
                attachment_id = str(raw_part.get("attachment_id") or "")
                # 变量说明：data_url 表示data 的访问地址。
                data_url = attachment_store.data_url(attachment_id) if attachment_store is not None else None
                if not data_url:
                    raise ModelConfigurationError("会话图片附件不可用，无法安全构造模型输入")
                parts.append({"type": "image_url", "image_url": {"url": data_url}})
            hydrated.append({**provider_message, "content": parts})

        if message.get("role") == "tool" and isinstance(content, str):
            try:
                # 变量说明：tool_payload 表示当前步骤使用的 tool_payload 值。
                tool_payload = json.loads(content)
            except (TypeError, ValueError):
                # 变量说明：tool_payload 表示当前步骤使用的 tool_payload 值。
                tool_payload = {}
            # 变量说明：metadata 表示当前步骤使用的 metadata 值。
            metadata = tool_payload.get("metadata") if isinstance(tool_payload, Mapping) else None
            # 变量说明：image_ref 表示当前步骤使用的 image_ref 值。
            image_ref = metadata.get("model_image_ref") if isinstance(metadata, Mapping) else None
            if isinstance(image_ref, Mapping) and str(image_ref.get("id") or ""):
                pending_tool_images.append(image_ref)

        # 变量说明：next_is_tool 表示当前步骤使用的 next_is_tool 值。
        next_is_tool = (
            index + 1 < len(messages)
            and messages[index + 1].get("role") == "tool"
        )
        if pending_tool_images and not next_is_tool:
            # 变量说明：observation_parts 表示当前流程使用的 observation_parts 集合。
            observation_parts: list[dict[str, Any]] = [{
                "type": "text",
                "text": "附件工具生成了以下会话内图片，请直接观察图片继续分析。",
            }]
            for image_ref in pending_tool_images:
                # 变量说明：attachment_id 表示attachment 对象的唯一标识。
                attachment_id = str(image_ref.get("id") or "")
                # 变量说明：data_url 表示data 的访问地址。
                data_url = attachment_store.data_url(attachment_id) if attachment_store is not None else None
                if not data_url:
                    raise ModelConfigurationError("PDF 派生图片不可用，无法安全构造模型输入")
                observation_parts.append({"type": "image_url", "image_url": {"url": data_url}})
            hydrated.append({"role": "user", "content": observation_parts})
            pending_tool_images.clear()
    return hydrated


# 函数职责：完成 bind_attachment_store 对应的业务处理。
# 参数关系：model_call 表示当前步骤使用的 model_call 值；attachment_store 表示当前步骤使用的 attachment_store 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def bind_attachment_store(model_call: Any, attachment_store: Any | None) -> Any:
    """Hydrate session-private image refs without changing provider factory APIs."""

    if attachment_store is None:
        return model_call

    # 函数职责：异步完成 call_with_attachments 对应的业务处理。
    # 参数关系：kwargs 表示当前流程使用的 kwargs 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def call_with_attachments(**kwargs: Any) -> Any:
        # 变量说明：messages 表示发送给模型或客户端的消息序列。
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            # 变量说明：kwargs 表示当前流程使用的 kwargs 集合。
            kwargs = {
                **kwargs,
                "messages": _hydrate_attachment_messages(messages, attachment_store),
            }
        return await model_call(**kwargs)

    # 变量说明：manages_retries 表示当前流程使用的 manages_retries 集合。
    call_with_attachments.manages_retries = getattr(model_call, "manages_retries", False)
    # 附件包装只转换输入，不能改变 runtime 选择结构化输出回调的能力判断。
    call_with_attachments.emits_assistant_items = getattr(model_call, "emits_assistant_items", False)
    return call_with_attachments



# 函数职责：构建 model_call 对应的数据或流程。
# 参数关系：config 表示当前生效的配置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def build_model_call(config: ProviderConfig):
    from src.model.request import create_model_call
    return create_model_call(config, credentials=get_api_key)
