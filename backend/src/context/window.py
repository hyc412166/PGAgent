"""Token estimation and provider tool-protocol validation.

Prompt assembly and full transcript replacement live in ``context_service``.
This module deliberately has no task anchors, message trimming, micro-
compaction, summaries, memories, epochs, or dynamic preambles.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any, Mapping, Sequence


Message = dict[str, Any]
DEFAULT_CONTEXT_LIMIT_TOKENS = 200_000
DEFAULT_COMPACT_THRESHOLD_TOKENS = 180_000


def estimate_tokens(value: str | Mapping[str, Any] | Sequence[Any]) -> int:
    """Return a conservative provider-neutral estimate for input budgeting."""

    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    non_ascii = sum(1 for character in value if ord(character) > 127)
    ascii_count = len(value) - non_ascii
    return max(1, (non_ascii * 2) + math.ceil(ascii_count / 4))


def message_tokens(message: Mapping[str, Any]) -> int:
    content = message.get("content")
    image_tokens = 0
    if isinstance(content, list):
        image_tokens = 1_200 * sum(
            1 for item in content
            if isinstance(item, Mapping) and item.get("type") == "pgagent_image_ref"
        )
    return 4 + estimate_tokens(dict(message)) + image_tokens


class ContextManager:
    """Own the context limit and remove only provider-invalid tool groups."""

    def __init__(self, *, max_tokens: int = DEFAULT_CONTEXT_LIMIT_TOKENS, **_ignored: Any) -> None:
        if max_tokens < 256:
            raise ValueError("max_tokens must be at least 256")
        self.max_tokens = int(max_tokens)

    @staticmethod
    def _conversation_groups(messages: Sequence[Mapping[str, Any]]) -> list[list[Message]]:
        groups: list[list[Message]] = []
        index = 0
        while index < len(messages):
            current = dict(messages[index])
            index += 1
            if current.get("role") == "tool":
                # A detached tool result is invalid at the provider boundary.
                continue
            calls = current.get("tool_calls")
            if current.get("role") != "assistant" or not isinstance(calls, list) or not calls:
                groups.append([current])
                continue

            results: list[Message] = []
            while index < len(messages) and messages[index].get("role") == "tool":
                results.append(dict(messages[index]))
                index += 1
            required = Counter(
                str(call.get("id") or "")
                for call in calls
                if isinstance(call, Mapping)
            )
            actual = Counter(str(result.get("tool_call_id") or "") for result in results)
            if (
                required
                and required == actual
                and "" not in required
                and all(count == 1 for count in required.values())
            ):
                groups.append([current, *results])
        return groups

    def repair_provider_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> tuple[list[Message], dict[str, Any]]:
        """Drop corrupt historical tool batches without changing valid content."""

        normalized = [dict(message) for message in messages]
        cursor = 0
        while cursor < len(normalized) and normalized[cursor].get("role") == "system":
            cursor += 1
        repaired = [
            *normalized[:cursor],
            *[
                message
                for group in self._conversation_groups(normalized[cursor:])
                for message in group
            ],
        ]
        if repaired == normalized:
            return repaired, {"removed_messages": 0, "affected_call_ids": []}

        def identity(message: Mapping[str, Any]) -> str:
            return json.dumps(dict(message), ensure_ascii=False, sort_keys=True, default=str)

        retained = Counter(identity(message) for message in repaired)
        removed: list[Message] = []
        for message in normalized:
            key = identity(message)
            if retained[key] > 0:
                retained[key] -= 1
            else:
                removed.append(message)
        affected: set[str] = set()
        for message in removed:
            call_id = str(message.get("tool_call_id") or "")
            if call_id:
                affected.add(call_id)
            for call in message.get("tool_calls") or []:
                if isinstance(call, Mapping) and call.get("id"):
                    affected.add(str(call["id"]))
        return repaired, {
            "removed_messages": len(removed),
            "affected_call_ids": sorted(affected),
        }
