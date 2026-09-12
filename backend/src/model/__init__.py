"""Model-provider configuration and request gateway."""
# 文件职责：负责模型连接、请求、协议转换与流式响应中的 __init__ 子模块。
# 逻辑关系：上层通过 model/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from .gateway import ModelConfigurationError, PartialModelStreamError, ProviderConfig, build_model_call
from .output import (
    AssistantMessageItem,
    EndTurn,
    EndTurnValue,
    HostedToolItem,
    LocalToolCallItem,
    ModelResponseStatus,
    NormalizedModelResponse,
    NormalizedOutputItem,
    NormalizedResponse,
    OutputItem,
    OutputPhase,
    ReasoningItem,
    ResponseStatus,
)

# 变量说明：__all__ 表示当前步骤使用的 __all__ 值。
__all__ = [
    "ModelConfigurationError",
    "PartialModelStreamError",
    "ProviderConfig",
    "build_model_call",
    "AssistantMessageItem",
    "EndTurn",
    "EndTurnValue",
    "HostedToolItem",
    "LocalToolCallItem",
    "ModelResponseStatus",
    "NormalizedModelResponse",
    "NormalizedOutputItem",
    "NormalizedResponse",
    "OutputItem",
    "OutputPhase",
    "ReasoningItem",
    "ResponseStatus",
]
