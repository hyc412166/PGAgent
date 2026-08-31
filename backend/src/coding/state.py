"""Run-scoped coding evidence captured from successful tool invocations."""

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
class CodingSessionState:
    """Small durable ledger; patch bodies and command output stay in normal tool results."""

    changes: list[dict[str, Any]] = field(default_factory=list)
    validations: list[dict[str, Any]] = field(default_factory=list)
    revision: int = 0

    @classmethod
    def restore(cls, value: Mapping[str, Any] | None) -> "CodingSessionState":
        payload = dict(value or {})
        changes = _mapping_items(payload.get("changes"))
        validations = _mapping_items(payload.get("validations"))
        revision = max([
            int(payload.get("revision") or 0),
            *(int(item.get("revision") or 0) for item in (*changes, *validations)),
        ])
        return cls(changes=changes, validations=validations, revision=revision)

    async def after_invoke(
        self,
        invocation: ToolInvocation,
        result: ToolResult,
        _context: HookContext,
    ) -> ToolResult:
        change_set = result.metadata.get("change_set")
        if result.changed and not isinstance(change_set, Mapping):
            path = str(result.metadata.get("path") or invocation.arguments.get("path") or "").strip()
            if path:
                operation = str(result.metadata.get("operation") or "").strip() or {
                    "delete": "delete",
                    "write": "update",
                    "write_file": "update",
                    "edit": "update",
                    "edit_file": "update",
                }.get(invocation.wire_name)
                if operation:
                    change_set = {
                        "status": "observed",
                        "source": "file_tool",
                        "file_count": 1,
                        "files": [{"path": path.replace("\\", "/"), "operation": operation}],
                    }
        if result.changed and isinstance(change_set, Mapping):
            self.revision += 1
            self.changes.append({
                "sequence": len(self.changes) + 1,
                "revision": self.revision,
                "call_id": invocation.call_id,
                "tool": invocation.wire_name,
                **dict(change_set),
            })

        validation = result.metadata.get("validation")
        if isinstance(validation, Mapping):
            self.revision += 1
            self.validations.append({
                "sequence": len(self.validations) + 1,
                "revision": self.revision,
                "call_id": invocation.call_id,
                "tool": invocation.wire_name,
                **dict(validation),
            })
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "changes": [dict(item) for item in self.changes],
            "validations": [dict(item) for item in self.validations],
            "revision": self.revision,
        }

    def prompt_summary(self) -> str:
        if not self.changes and not self.validations:
            return ""
        touched: list[str] = []
        for record in self.changes:
            for item in record.get("files") or []:
                if isinstance(item, Mapping):
                    path = str(item.get("path") or "").strip()
                    if path and path not in touched:
                        touched.append(path)
        lines = ["## Current coding evidence"]
        if touched:
            lines.append("Changed paths: " + ", ".join(touched))
        latest_change_revision = max(
            (int(item.get("revision") or 0) for item in self.changes),
            default=0,
        )
        latest_validation_revision = max(
            (int(item.get("revision") or 0) for item in self.validations),
            default=0,
        )
        if self.validations and latest_validation_revision > latest_change_revision:
            latest = self.validations[-1]
            lines.append(
                "Latest validation: "
                f"{latest.get('kind', 'other')} status={latest.get('status', 'unknown')} "
                f"cwd={latest.get('cwd', '.')} exit_code={latest.get('exit_code')}"
            )
        elif self.validations and self.changes:
            lines.append("Validation status: stale (changes were made after the latest validation)")
        elif self.changes:
            lines.append("Validation status: not_run")
        return "\n".join(lines)
