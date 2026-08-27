"""Agent execution loop, completion checks, retry behavior, and guards."""

from .completion import CompletionDecision, DeterministicAcceptanceReport, DeterministicCheck, decide_deterministic_completion, verify_deterministic_completion
from .engine import AgentRuntime, ModelToolCall, ModelTurn, RunOutcome, RuntimeConfig, empty_usage, merge_usage, normalize_usage
from .errors import APIErrorKind, call_with_retry, classify_api_error
from .guards import GuardDecision, LoopGuard

__all__ = [
    "APIErrorKind", "AgentRuntime", "CompletionDecision", "DeterministicAcceptanceReport",
    "DeterministicCheck", "GuardDecision", "LoopGuard", "ModelToolCall", "ModelTurn",
    "RunOutcome", "RuntimeConfig", "call_with_retry", "classify_api_error",
    "decide_deterministic_completion", "empty_usage", "merge_usage", "normalize_usage",
    "verify_deterministic_completion",
]
