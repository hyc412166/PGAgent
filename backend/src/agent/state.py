"""Mutable state carried through one AgentRuntime execution."""

from __future__ import annotations

from typing import Any, TypedDict


class RunState(TypedDict, total=False):
    status: str
    mode: str
    messages: list[dict[str, Any]]
    events: list[dict[str, Any]]
    output: str | None
    error: str | None
    stop_reason: str | None
    pending_approval: dict[str, Any] | None
    current_made_progress: bool
    seen_observations: list[str]
    usage: dict[str, Any]
    context: dict[str, Any]
    compaction_state: dict[str, Any]
    context_artifact_refs: list[dict[str, Any]]
    transcript_delta: list[dict[str, Any]]
    verification_trace: list[dict[str, Any]]
    compaction_count: int
    context_overflow_retries: int
    completion_verification_attempts: int
    acceptance_report: dict[str, Any]
    memory_citation: dict[str, Any]
    prompt_cache_key: str
