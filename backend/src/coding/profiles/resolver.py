"""Resolve configured workflow identities without creating separate runtimes."""

from __future__ import annotations

from collections.abc import Iterable

from .base import WorkflowProfile
from .coding import CODING_PROFILE
from .debug import DEBUG_PROFILE
from .review import REVIEW_PROFILE


GENERAL_PROFILE_ID = "general"
AUTO_PROFILE_ID = "auto"
WORKFLOW_PROFILE_IDS = frozenset({
    AUTO_PROFILE_ID,
    GENERAL_PROFILE_ID,
    CODING_PROFILE.id,
    REVIEW_PROFILE.id,
    DEBUG_PROFILE.id,
})

_EXPLICIT_PROFILES = {
    CODING_PROFILE.id: CODING_PROFILE,
    REVIEW_PROFILE.id: REVIEW_PROFILE,
    DEBUG_PROFILE.id: DEBUG_PROFILE,
}


def resolve_workflow_profile(
    profile_id: object,
    enabled_tool_names: Iterable[str],
) -> WorkflowProfile | None:
    """Resolve only explicit workflows; ``auto`` uses the compact general surface."""

    requested = str(profile_id or AUTO_PROFILE_ID).strip().casefold()
    if requested in _EXPLICIT_PROFILES:
        return _EXPLICIT_PROFILES[requested]
    if requested == GENERAL_PROFILE_ID:
        return None
    return None
