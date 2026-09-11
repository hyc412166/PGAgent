"""Connection-level wire protocol configuration."""
# 文件职责：负责模型连接、请求、协议转换与流式响应中的 config 子模块。
# 逻辑关系：上层通过 model/config.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

# 类职责：定义 ProviderConfig 在本领域中的数据与行为。
@dataclass(slots=True)
class ProviderConfig:
    # 变量说明：provider 表示模型供应商。
    provider: str
    # 变量说明：base_url 表示base 的访问地址。
    base_url: str
    # 变量说明：secret_ref 表示当前步骤使用的 secret_ref 值。
    secret_ref: str
    # 变量说明：model_id 表示model 对象的唯一标识。
    model_id: str
    # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
    model_connection_id: str | None = None
    # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
    thinking_level: str = "medium"
    # 变量说明：custom_headers 表示当前流程使用的 custom_headers 集合。
    custom_headers: dict[str, str] = field(default_factory=dict)
    # 变量说明：api_protocol 表示当前步骤使用的 api_protocol 值。
    api_protocol: Literal["chat_completions", "responses"] = "chat_completions"
    # 变量说明：max_output_tokens 表示当前流程使用的 max_output_tokens 集合。
    max_output_tokens: int = 8_000
