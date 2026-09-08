"""Compatibility facade for the domain-oriented run service package.

This module is the explicit public facade for run coordination. Internal
modules live beside it under :mod:`src.runs`.
"""
# 文件职责：负责运行创建、恢复、流式传输和生命周期中的 service 子模块。
# 逻辑关系：上层通过 runs/service.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from src.config import settings

from src.model.gateway import build_model_call
from src.context.compaction import _message_payload, _prepare_session_history, approval_matches_pending
from src.runs.lifecycle import (
    ACTIVE_STATUSES,
    STOPPABLE_STATUSES,
    USER_INTERRUPT_ERROR,
    USER_INTERRUPT_REASON,
    USER_INTERRUPT_REASONS,
    RunCoordinator,
    coordinator,
)
from src.runs.delegation import _SubagentTaskDelegate

# 变量说明：__all__ 表示当前步骤使用的 __all__ 值。
__all__ = [
    "ACTIVE_STATUSES",
    "STOPPABLE_STATUSES",
    "USER_INTERRUPT_ERROR",
    "USER_INTERRUPT_REASON",
    "USER_INTERRUPT_REASONS",
    "RunCoordinator",
    "_SubagentTaskDelegate",
    "_message_payload",
    "_prepare_session_history",
    "approval_matches_pending",
    "build_model_call",
    "coordinator",
    "settings",
]
