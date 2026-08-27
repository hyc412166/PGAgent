"""Session-bound, paginated access to runtime artifacts."""

from __future__ import annotations

import json

from src.context.assembly import ArtifactStore, DEFAULT_TOOL_OUTPUT_MAX_CHARS
from src.tools.types import ToolResult


DEFAULT_ARTIFACT_PAGE_CHARS = 20_000
MAX_ARTIFACT_PAGE_CHARS = 24_000
MAX_SERIALIZED_ARTIFACT_RESULT_CHARS = DEFAULT_TOOL_OUTPUT_MAX_CHARS - 1_000


class ArtifactToolStore:
    """Expose only artifacts addressable by one runtime's bound store."""

    def __init__(self, artifact_store: ArtifactStore, *, max_page_chars: int = MAX_ARTIFACT_PAGE_CHARS) -> None:
        self.artifact_store = artifact_store
        self.max_page_chars = max(1, int(max_page_chars))

    def read(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int = DEFAULT_ARTIFACT_PAGE_CHARS,
    ) -> ToolResult:
        try:
            start = int(offset)
            requested = int(limit)
        except (TypeError, ValueError):
            return ToolResult(
                "read_artifact",
                False,
                "offset and limit must be integers",
                error_code="invalid_arguments",
            )
        if start < 0 or requested < 1 or requested > self.max_page_chars:
            return ToolResult(
                "read_artifact",
                False,
                f"offset must be non-negative and limit must be between 1 and {self.max_page_chars}",
                error_code="invalid_arguments",
            )
        try:
            payload = self.artifact_store.get(str(artifact_id or ""))
        except (OSError, ValueError):
            payload = None
        if payload is None:
            return ToolResult(
                "read_artifact",
                False,
                "Artifact is not available in this session",
                error_code="artifact_not_found",
            )
        text = payload.decode("utf-8", errors="replace")
        maximum_end = min(len(text), start + requested)

        def page(end: int) -> ToolResult:
            chunk = text[start:end]
            eof = end >= len(text)
            return ToolResult(
                "read_artifact",
                True,
                chunk,
                metadata={
                    "artifact_id": str(artifact_id),
                    "offset": start,
                    "next_offset": None if eof else end,
                    "total_chars": len(text),
                    "eof": eof,
                },
            )

        candidate = page(maximum_end)
        if len(json.dumps(candidate.to_dict(), ensure_ascii=False)) < MAX_SERIALIZED_ARTIFACT_RESULT_CHARS:
            return candidate
        low = min(start + 1, maximum_end)
        high = maximum_end
        best = page(start)
        while low <= high:
            middle = (low + high) // 2
            current = page(middle)
            if len(json.dumps(current.to_dict(), ensure_ascii=False)) < MAX_SERIALIZED_ARTIFACT_RESULT_CHARS:
                best = current
                low = middle + 1
            else:
                high = middle - 1
        return best
