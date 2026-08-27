"""Shared value objects used by tools and the agent runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class ApprovalRequest:
    """A side-effecting tool call waiting for an explicit user decision."""

    tool_name: str
    arguments: dict[str, Any]
    reason: str
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolResult:
    """Normalized result returned by every built-in tool."""

    tool_name: str
    ok: bool
    content: str = ""
    changed: bool = False
    approval_required: bool = False
    approval_request: ApprovalRequest | None = None
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def made_progress(self) -> bool:
        return self.ok and (self.changed or bool(self.content.strip()))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return value
