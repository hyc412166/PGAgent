"""Run-scoped structured evidence preserved across approval and recovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from src.tools.hooks import HookContext
from src.tools.invocation import ToolInvocation
from src.tools.types import ToolResult


def _mapping_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


@dataclass(slots=True)
class WorkflowEvidenceState:
    """Compact ledger of model-recorded review findings and debug evidence."""

    review_findings: list[dict[str, Any]] = field(default_factory=list)
    debug_evidence: list[dict[str, Any]] = field(default_factory=list)
    sequence: int = 0

    @classmethod
    def restore(cls, value: Mapping[str, Any] | None) -> "WorkflowEvidenceState":
        payload = dict(value or {})
        findings = _mapping_items(payload.get("review_findings"))
        evidence = _mapping_items(payload.get("debug_evidence"))
        sequence = max([
            int(payload.get("sequence") or 0),
            *(int(item.get("sequence") or 0) for item in (*findings, *evidence)),
        ])
        return cls(review_findings=findings, debug_evidence=evidence, sequence=sequence)

    async def after_invoke(
        self,
        invocation: ToolInvocation,
        result: ToolResult,
        _context: HookContext,
    ) -> ToolResult:
        evidence = result.metadata.get("workflow_evidence")
        if not result.ok or not isinstance(evidence, Mapping):
            return result
        self.sequence += 1
        record = {
            "sequence": self.sequence,
            "call_id": invocation.call_id,
            **dict(evidence),
        }
        if evidence.get("kind") == "review_finding":
            self.review_findings.append(record)
        elif evidence.get("kind") == "debug_evidence":
            self.debug_evidence.append(record)
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "review_findings": [dict(item) for item in self.review_findings],
            "debug_evidence": [dict(item) for item in self.debug_evidence],
            "sequence": self.sequence,
        }

    def prompt_summary(self, profile_id: str) -> str:
        if profile_id == "review" and self.review_findings:
            lines = ["## Recorded review findings"]
            for item in self.review_findings:
                location = str(item.get("path") or "")
                if item.get("line") is not None:
                    location += f":{item['line']}"
                lines.append(
                    f"- [{item.get('severity', 'unknown')}] {item.get('title', '')} "
                    f"({location or 'location not recorded'})"
                )
                lines.append(f"  Failure scenario: {item.get('failure_scenario', '')}")
                lines.append(f"  Evidence: {item.get('evidence', '')}")
                if item.get("suggested_fix"):
                    lines.append(f"  Suggested fix: {item['suggested_fix']}")
                if item.get("test_gap"):
                    lines.append(f"  Test gap: {item['test_gap']}")
            return "\n".join(lines)
        if profile_id == "debug" and self.debug_evidence:
            lines = ["## Recorded debug evidence"]
            for item in self.debug_evidence:
                lines.append(
                    f"- {item.get('stage', 'observation')} "
                    f"status={item.get('status', 'unknown')}: {item.get('summary', '')}"
                )
                location = str(item.get("path") or "")
                if item.get("line") is not None:
                    location += f":{item['line']}"
                if location:
                    lines.append(f"  Location: {location}")
                if item.get("command"):
                    lines.append(f"  Command: {item['command']}")
                if item.get("details"):
                    lines.append(f"  Details: {item['details']}")
            return "\n".join(lines)
        return ""
