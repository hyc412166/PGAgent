"""Deterministic safeguards around the otherwise dynamic agent loop."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GuardDecision:
    stop: bool
    code: str | None = None
    reason: str | None = None

    @classmethod
    def continue_(cls) -> "GuardDecision":
        return cls(False)


class LoopGuard:
    """Track optional hard limits and deterministic anti-loop signals."""

    def __init__(
        self,
        *,
        max_steps: int | None = 0,
        max_calls: int | None = 0,
        identical_limit: int = 3,
        no_progress_limit: int = 4,
    ) -> None:
        if max_steps is not None and max_steps < 0:
            raise ValueError("max_steps 必须大于等于 0")
        if max_calls is not None and max_calls < 0:
            raise ValueError("max_calls 必须大于等于 0")
        if min(identical_limit, no_progress_limit) < 1:
            raise ValueError("重复调用和无进展限制必须大于 0")
        self.max_steps = max_steps
        self.max_calls = max_calls
        self.identical_limit = identical_limit
        self.no_progress_limit = no_progress_limit
        self.steps = 0
        self.calls = 0
        self.identical_count = 0
        self.no_progress_count = 0
        self._last_call_fingerprint: str | None = None

    @staticmethod
    def call_fingerprint(tool_name: str, arguments: dict[str, Any] | None) -> str:
        canonical = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(f"{tool_name}\0{canonical}".encode("utf-8")).hexdigest()

    def before_step(self) -> GuardDecision:
        if self.max_steps and self.steps >= self.max_steps:
            return GuardDecision(True, "max_steps", f"已达到最大步骤数 {self.max_steps}")
        self.steps += 1
        return GuardDecision.continue_()

    def before_tool_call(self, tool_name: str, arguments: dict[str, Any] | None) -> GuardDecision:
        if self.max_calls and self.calls >= self.max_calls:
            return GuardDecision(True, "max_tool_calls", f"已达到最大工具调用数 {self.max_calls}")

        fingerprint = self.call_fingerprint(tool_name, arguments)
        if fingerprint == self._last_call_fingerprint:
            self.identical_count += 1
        else:
            self._last_call_fingerprint = fingerprint
            self.identical_count = 1
        self.calls += 1

        if self.identical_count >= self.identical_limit:
            return GuardDecision(
                True,
                "repeated_tool_call",
                f"相同工具和参数已连续出现 {self.identical_count} 次",
            )
        return GuardDecision.continue_()

    # Kept as an intuitive alias for callers and tests.
    record_tool_call = before_tool_call

    def record_progress(self, made_progress: bool) -> GuardDecision:
        if made_progress:
            self.no_progress_count = 0
        else:
            self.no_progress_count += 1
        if self.no_progress_count >= self.no_progress_limit:
            return GuardDecision(
                True,
                "no_progress",
                f"已连续 {self.no_progress_count} 步没有产生有效进展",
            )
        return GuardDecision.continue_()

    def snapshot(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "calls": self.calls,
            "identical_count": self.identical_count,
            "no_progress_count": self.no_progress_count,
            "last_call_fingerprint": self._last_call_fingerprint,
        }

    def restore(self, snapshot: dict[str, Any] | None) -> None:
        """Restore counters from a persisted approval/checkpoint boundary."""

        if not snapshot:
            return
        steps = max(int(snapshot.get("steps", 0)), 0)
        calls = max(int(snapshot.get("calls", 0)), 0)
        self.steps = min(steps, self.max_steps) if self.max_steps else steps
        self.calls = min(calls, self.max_calls) if self.max_calls else calls
        self.identical_count = min(max(int(snapshot.get("identical_count", 0)), 0), self.identical_limit)
        self.no_progress_count = min(max(int(snapshot.get("no_progress_count", 0)), 0), self.no_progress_limit)
        fingerprint = snapshot.get("last_call_fingerprint")
        self._last_call_fingerprint = str(fingerprint) if fingerprint else None
