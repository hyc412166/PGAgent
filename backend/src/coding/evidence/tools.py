"""Provider-callable helpers that emit structured review and debug evidence."""

from __future__ import annotations

import json
from typing import Any

from src.tools.sandbox import WorkspaceSandbox
from src.tools.types import ToolResult


def record_review_finding(
    _sandbox: WorkspaceSandbox,
    *,
    severity: str,
    title: str,
    path: str,
    failure_scenario: str,
    evidence: str,
    line: int | None = None,
    suggested_fix: str = "",
    test_gap: str = "",
) -> ToolResult:
    finding: dict[str, Any] = {
        "kind": "review_finding",
        "severity": severity,
        "title": title,
        "path": path,
        "failure_scenario": failure_scenario,
        "evidence": evidence,
    }
    if line is not None:
        finding["line"] = line
    if suggested_fix:
        finding["suggested_fix"] = suggested_fix
    if test_gap:
        finding["test_gap"] = test_gap
    return ToolResult(
        "review_finding",
        True,
        json.dumps(finding, ensure_ascii=False),
        metadata={"workflow_evidence": finding},
    )


def record_debug_evidence(
    _sandbox: WorkspaceSandbox,
    *,
    stage: str,
    summary: str,
    status: str,
    path: str = "",
    line: int | None = None,
    command: str = "",
    details: str = "",
) -> ToolResult:
    evidence: dict[str, Any] = {
        "kind": "debug_evidence",
        "stage": stage,
        "summary": summary,
        "status": status,
    }
    if path:
        evidence["path"] = path
    if line is not None:
        evidence["line"] = line
    if command:
        evidence["command"] = command
    if details:
        evidence["details"] = details
    return ToolResult(
        "debug_evidence",
        True,
        json.dumps(evidence, ensure_ascii=False),
        metadata={"workflow_evidence": evidence},
    )
