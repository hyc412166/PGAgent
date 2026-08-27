"""Compatibility facade for the domain-oriented run service package.

This module is the explicit public facade for run coordination. Internal
modules live beside it under :mod:`src.runs`.
"""

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
