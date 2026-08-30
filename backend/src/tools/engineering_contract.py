"""Provider contracts for engineering workflow evidence tools."""

from __future__ import annotations

from typing import Any


ENGINEERING_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "review_finding": {
        "description": "Record one actionable code-review finding with a concrete failure scenario.",
        "parameters": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["critical", "important", "minor"]},
                "title": {"type": "string"},
                "path": {"type": "string"},
                "line": {"type": "integer", "minimum": 1},
                "failure_scenario": {"type": "string"},
                "evidence": {"type": "string"},
                "suggested_fix": {"type": "string"},
                "test_gap": {"type": "string"},
            },
            "required": ["severity", "title", "path", "failure_scenario", "evidence"],
        },
    },
    "debug_evidence": {
        "description": "Record one debugging observation, hypothesis, root cause, or regression result.",
        "parameters": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": [
                        "symptom",
                        "reproduction",
                        "observation",
                        "hypothesis",
                        "root_cause",
                        "regression",
                    ],
                },
                "summary": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [
                        "confirmed",
                        "supported",
                        "unconfirmed",
                        "disproved",
                        "passed",
                        "failed",
                    ],
                },
                "path": {"type": "string"},
                "line": {"type": "integer", "minimum": 1},
                "command": {"type": "string"},
                "details": {"type": "string"},
            },
            "required": ["stage", "summary", "status"],
        },
    },
}

ENGINEERING_TOOL_NAMES = tuple(ENGINEERING_TOOL_SCHEMAS)
