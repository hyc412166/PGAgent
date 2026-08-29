"""Serialize and restore the durable continuation state of an AgentRuntime."""

from __future__ import annotations

from typing import Any

from src.agent import AgentRuntime, RunOutcome, normalize_usage
from src.context.compaction import _json_safe
from src.tools.registry import ToolRegistry


class RunContinuationCodec:
    @staticmethod
    def capture_runtime_binding(
        base: dict[str, Any],
        runtime: AgentRuntime,
    ) -> dict[str, Any]:
        return {
            **dict(base),
            **runtime.tool_registry.runtime_state(),
        }

    @staticmethod
    def capture_registry_binding(
        base: dict[str, Any],
        registry: ToolRegistry,
    ) -> dict[str, Any]:
        return {
            **dict(base),
            **registry.runtime_state(),
        }

    @staticmethod
    def snapshot(outcome: RunOutcome) -> dict[str, Any]:
        messages = [] if outcome.stop_reason == "acceptance_failed" else outcome.messages
        return _json_safe({
            "status": outcome.status,
            "output": outcome.output,
            "messages": messages,
            "events": outcome.events,
            "steps": outcome.steps,
            "tool_calls": outcome.tool_calls,
            "mode": outcome.mode,
            "stop_reason": outcome.stop_reason,
            "error": outcome.error,
            "pending_approval": outcome.pending_approval,
            "guard_snapshot": outcome.guard_snapshot,
            "usage": outcome.usage,
            "active_elapsed_seconds": outcome.active_elapsed_seconds,
            "runtime_binding": outcome.runtime_binding,
            "compaction_state": outcome.compaction_state,
            "artifact_refs": outcome.artifact_refs,
            "transcript_delta": outcome.transcript_delta,
            "verification_trace": outcome.verification_trace,
            "acceptance_report": dict(outcome.acceptance_report),
            "completion_verification_attempts": outcome.completion_verification_attempts,
            "memory_citation": outcome.memory_citation,
        })

    @staticmethod
    def restore(payload: dict[str, Any]) -> RunOutcome:
        return RunOutcome(
            status=str(payload.get("status") or "failed"),
            output=payload.get("output"),
            messages=list(payload.get("messages") or []),
            events=list(payload.get("events") or []),
            steps=int(payload.get("steps") or 0),
            tool_calls=int(payload.get("tool_calls") or 0),
            mode=str(payload.get("mode") or "auto"),
            stop_reason=payload.get("stop_reason"),
            error=payload.get("error"),
            pending_approval=payload.get("pending_approval"),
            guard_snapshot=dict(payload.get("guard_snapshot") or {}),
            usage=normalize_usage(payload.get("usage")),
            active_elapsed_seconds=max(0.0, float(payload.get("active_elapsed_seconds") or 0.0)),
            runtime_binding=dict(payload.get("runtime_binding") or {}),
            compaction_state=dict(payload.get("compaction_state") or {}),
            artifact_refs=[dict(item) for item in payload.get("artifact_refs") or [] if isinstance(item, dict)],
            transcript_delta=(
                [dict(item) for item in payload.get("transcript_delta") or [] if isinstance(item, dict)]
                if "transcript_delta" in payload
                else None
            ),
            verification_trace=[
                dict(item) for item in payload.get("verification_trace") or [] if isinstance(item, dict)
            ],
            acceptance_report=dict(payload.get("acceptance_report") or {}),
            completion_verification_attempts=max(
                0,
                int(payload.get("completion_verification_attempts") or 0),
            ),
            memory_citation=dict(payload.get("memory_citation") or {}),
        )
