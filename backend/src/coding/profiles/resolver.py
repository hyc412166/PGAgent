"""Resolve configured workflow identities without creating separate runtimes."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 resolver 子模块。
# 逻辑关系：上层通过 coding/profiles/resolver.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from collections.abc import Iterable

from .base import WorkflowProfile
from .coding import CODING_PROFILE
from .debug import DEBUG_PROFILE
from .review import REVIEW_PROFILE


# 变量说明：GENERAL_PROFILE_ID 表示GENERAL_PROFILE 对象的唯一标识。
GENERAL_PROFILE_ID = "general"
# 变量说明：AUTO_PROFILE_ID 表示AUTO_PROFILE 对象的唯一标识。
AUTO_PROFILE_ID = "auto"
# 变量说明：WORKFLOW_PROFILE_IDS 表示WORKFLOW_PROFILE 对象标识集合。
WORKFLOW_PROFILE_IDS = frozenset({
    AUTO_PROFILE_ID,
    GENERAL_PROFILE_ID,
    CODING_PROFILE.id,
    REVIEW_PROFILE.id,
    DEBUG_PROFILE.id,
})

# 变量说明：_EXPLICIT_PROFILES 表示当前流程使用的 _EXPLICIT_PROFILES 集合。
_EXPLICIT_PROFILES = {
    CODING_PROFILE.id: CODING_PROFILE,
    REVIEW_PROFILE.id: REVIEW_PROFILE,
    DEBUG_PROFILE.id: DEBUG_PROFILE,
}


# 函数职责：解析 workflow_profile 对应的数据或流程。
# 参数关系：profile_id 表示profile 对象的唯一标识；enabled_tool_names 表示当前流程使用的 enabled_tool_names 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def resolve_workflow_profile(
    profile_id: object,
    enabled_tool_names: Iterable[str],
) -> WorkflowProfile | None:
    """Resolve only explicit workflows; ``auto`` uses the compact general surface."""

    # 变量说明：requested 表示当前步骤使用的 requested 值。
    requested = str(profile_id or AUTO_PROFILE_ID).strip().casefold()
    if requested in _EXPLICIT_PROFILES:
        return _EXPLICIT_PROFILES[requested]
    if requested == GENERAL_PROFILE_ID:
        return None
    return None
