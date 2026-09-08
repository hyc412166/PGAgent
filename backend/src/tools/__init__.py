"""Sandboxed tools available to PGAgent."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 __init__ 子模块。
# 逻辑关系：上层通过 tools/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
"""PGAgent built-in tools and workspace sandbox."""

from .registry import ToolRegistry, create_default_registry
from .invocation import ApprovalGrant, ToolInvocation
from .name import ToolName
from .pipeline import InvocationPipeline
from .plan import ToolPlan, ToolPlanBuilder
from .router import ToolRouter
from .runtime import ToolRuntime
from .scheduler import InvocationBatchPlan, InvocationScheduler
from .types import ApprovalRequest, ToolResult

# 变量说明：__all__ 表示当前步骤使用的 __all__ 值。
__all__ = [
    "ApprovalGrant",
    "ApprovalRequest",
    "InvocationPipeline",
    "InvocationBatchPlan",
    "InvocationScheduler",
    "ToolInvocation",
    "ToolName",
    "ToolPlan",
    "ToolPlanBuilder",
    "ToolRegistry",
    "ToolResult",
    "ToolRouter",
    "ToolRuntime",
    "create_default_registry",
]
