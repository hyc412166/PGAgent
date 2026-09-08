"""Engineering workflow profiles for the shared AgentRuntime."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 __init__ 子模块。
# 逻辑关系：上层通过 coding/profiles/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from .base import WorkflowProfile
from .coding import CODING_PROFILE
from .debug import DEBUG_PROFILE
from .resolver import (
    AUTO_PROFILE_ID,
    GENERAL_PROFILE_ID,
    WORKFLOW_PROFILE_IDS,
    resolve_workflow_profile,
)
from .review import REVIEW_PROFILE

# 变量说明：__all__ 表示当前步骤使用的 __all__ 值。
__all__ = [
    "AUTO_PROFILE_ID",
    "CODING_PROFILE",
    "DEBUG_PROFILE",
    "GENERAL_PROFILE_ID",
    "REVIEW_PROFILE",
    "WORKFLOW_PROFILE_IDS",
    "WorkflowProfile",
    "resolve_workflow_profile",
]
