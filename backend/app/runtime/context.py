"""Token-aware layered context assembly."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


Message = dict[str, Any]
DEFAULT_CONTEXT_LIMIT_TOKENS = 100_000
DEFAULT_COMPACT_THRESHOLD_TOKENS = 90_000


def estimate_tokens(value: str | Mapping[str, Any] | Sequence[Any]) -> int:
    """Provider-neutral conservative token estimate suitable for budgeting."""

    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    # Conservatively count non-ASCII code points as two tokens each. Claude-family
    # tokenizers can use two tokens for emoji/rare Unicode; provider-specific token
    # counters may later reclaim this safety margin.
    non_ascii = sum(1 for character in value if ord(character) > 127)
    ascii_count = len(value) - non_ascii
    return max(1, (non_ascii * 2) + math.ceil(ascii_count / 4))


def message_tokens(message: Mapping[str, Any]) -> int:
    return 4 + estimate_tokens(dict(message))


@dataclass(slots=True)
class ContextBundle:
    messages: list[Message]
    estimated_tokens: int
    omitted_messages: int = 0
    truncated: bool = False


class ContextManager:
    """Compose durable context layers while favoring the newest conversation."""

    def __init__(self, *, max_tokens: int = DEFAULT_CONTEXT_LIMIT_TOKENS, recent_ratio: float = 0.55) -> None:
        if max_tokens < 256:
            raise ValueError("上下文预算至少为 256 tokens")
        if not 0.2 <= recent_ratio <= 0.9:
            raise ValueError("recent_ratio 必须位于 0.2 到 0.9")
        self.max_tokens = max_tokens
        self.recent_ratio = recent_ratio

    @staticmethod
    def _system_section(title: str, content: str | None) -> Message | None:
        if not content or not content.strip():
            return None
        return {"role": "system", "content": f"## {title}\n{content.strip()}"}

    @staticmethod
    def _memory_text(memories: Iterable[str | Mapping[str, Any]]) -> str:
        rendered: list[str] = []
        for memory in memories:
            if isinstance(memory, str):
                value = memory.strip()
                pinned = False
            else:
                value = str(memory.get("content", "")).strip()
                pinned = bool(memory.get("pinned", False))
            if value:
                rendered.append(f"- {'[固定] ' if pinned else ''}{value}")
        return "\n".join(rendered)

    @staticmethod
    def _fit_text(text: str, token_budget: int) -> tuple[str, bool]:
        if estimate_tokens(text) <= token_budget:
            return text, False
        marker = "\n[内容已按上下文预算截断]"
        if token_budget <= estimate_tokens(marker):
            candidate = marker
            while candidate and estimate_tokens(candidate) > token_budget:
                candidate = candidate[:-1]
            return candidate, True
        # Work in characters but repeatedly verify because CJK byte length differs.
        limit = max(0, (token_budget - estimate_tokens(marker)) * 3)
        candidate = text[:limit]
        while candidate and estimate_tokens(candidate.rstrip() + marker) > token_budget:
            candidate = candidate[: max(0, len(candidate) - 128)]
        return candidate.rstrip() + marker, True

    @staticmethod
    def _fit_message(message: Mapping[str, Any], token_budget: int) -> tuple[Message | None, bool]:
        """Fit using the cost of the complete provider message, not text alone."""

        normalized = dict(message)
        if message_tokens(normalized) <= token_budget:
            return normalized, False
        content = str(normalized.get("content", ""))
        marker = "\n[内容已按上下文预算截断]"
        minimal = {**normalized, "content": marker}
        if message_tokens(minimal) > token_budget:
            return None, True
        low, high = 0, len(content)
        best = minimal
        while low <= high:
            middle = (low + high) // 2
            candidate = {**normalized, "content": content[:middle].rstrip() + marker}
            if message_tokens(candidate) <= token_budget:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best, True

    def build(
        self,
        *,
        system_prompt: str,
        agent_instructions: str | None = None,
        workspace_rules: str | None = None,
        summary: str | None = None,
        memories: Iterable[str | Mapping[str, Any]] = (),
        recent_messages: Sequence[Mapping[str, Any]] = (),
        tool_results: Sequence[Mapping[str, Any]] = (),
        max_tokens: int | None = None,
    ) -> ContextBundle:
        budget = max_tokens or self.max_tokens
        if budget < 256:
            raise ValueError("上下文预算至少为 256 tokens")

        messages: list[Message] = []
        truncated = False
        fixed_sections = [
            self._system_section("系统规则", system_prompt),
            self._system_section("Agent 配置", agent_instructions),
            self._system_section("工作区规则", workspace_rules),
        ]
        for section in fixed_sections:
            if section is None:
                continue
            remaining = budget - sum(message_tokens(item) for item in messages)
            fitted_message, cut = self._fit_message(section, remaining)
            if fitted_message is None:
                truncated = True
                break
            messages.append(fitted_message)
            truncated = truncated or cut
            if sum(message_tokens(item) for item in messages) >= budget:
                return ContextBundle(messages, sum(message_tokens(item) for item in messages), len(recent_messages), True)

        remaining = max(0, budget - sum(message_tokens(item) for item in messages))
        durable_budget = max(0, int(remaining * (1 - self.recent_ratio)))
        durable_parts: list[str] = []
        if summary:
            durable_parts.append(f"## 会话摘要\n{summary.strip()}")
        memory_text = self._memory_text(memories)
        if memory_text:
            durable_parts.append(f"## 分层记忆\n{memory_text}")
        if durable_parts and durable_budget > 8:
            durable_message, cut = self._fit_message(
                {"role": "system", "content": "\n\n".join(durable_parts)},
                durable_budget,
            )
            if durable_message is not None:
                messages.append(durable_message)
                truncated = truncated or cut
            else:
                truncated = True

        merged_dynamic, dropped_tool_messages = self._merge_tool_results(recent_messages, tool_results)
        combined = [*messages, *merged_dynamic]
        selected, omitted_dynamic = self.trim_runtime_messages(combined, max_tokens=budget)
        truncated = truncated or dropped_tool_messages > 0 or omitted_dynamic > 0 or len(selected) < len(combined)
        total = sum(message_tokens(item) for item in selected)
        return ContextBundle(
            messages=selected,
            estimated_tokens=total,
            omitted_messages=omitted_dynamic + dropped_tool_messages,
            truncated=truncated,
        )

    def compact_messages(self, messages: Sequence[Mapping[str, Any]], *, token_budget: int = 800) -> str:
        """Create a bounded digest that represents every compacted message.

        This is deliberately extractive rather than a model-generated summary.
        Equal per-message budgets prevent the global truncation that could retain
        only the oldest rows while the compaction cursor advanced past newer rows.
        A content digest still represents a message when its excerpt cannot fit.
        """

        entries: list[tuple[str, str, str]] = []
        for message in messages:
            role = str(message.get("role", "unknown"))
            content = str(message.get("content", "")).strip().replace("\n", " ")
            if content:
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
                entries.append((role, content, digest))
        if not entries:
            return ""

        transcript_identity = "\n".join(
            f"{role}\0{digest}" for role, _content, digest in entries
        )

        def whole_transcript_digest() -> str:
            transcript_digest = hashlib.sha256(transcript_identity.encode("utf-8")).hexdigest()
            fitted, _ = self._fit_text(
                f"{len(entries)} messages [sha256:{transcript_digest}]",
                token_budget,
            )
            return fitted

        newline_cost = estimate_tokens("\n" * max(0, len(entries) - 1))
        available = max(1, token_budget - newline_cost)
        if available < len(entries):
            return whole_transcript_digest()

        per_entry_budget = max(1, available // len(entries))
        if any(
            estimate_tokens(f"{role[:1]}:{digest}") > per_entry_budget
            for role, _content, digest in entries
        ):
            return whole_transcript_digest()
        lines: list[str] = []
        for role, content, digest in entries:
            suffix = f" [sha256:{digest}]"
            prefix = f"{role}: "
            if estimate_tokens(prefix + content) <= per_entry_budget:
                line = prefix + content
            else:
                content_budget = per_entry_budget - estimate_tokens(prefix) - estimate_tokens(suffix)
                if content_budget > 0:
                    excerpt, _ = self._fit_text(content, content_budget)
                    line = prefix + excerpt + suffix
                else:
                    line, _ = self._fit_text(f"{role[:1]}:{digest}", per_entry_budget)
            lines.append(line)
        rendered = "\n".join(lines)
        if estimate_tokens(rendered) <= token_budget:
            return rendered
        return whole_transcript_digest()

    @staticmethod
    def _conversation_groups(messages: Sequence[Message]) -> list[list[Message]]:
        """Group an assistant tool request with its tool results atomically."""

        groups: list[list[Message]] = []
        index = 0
        while index < len(messages):
            current = dict(messages[index])
            group = [current]
            index += 1
            if current.get("role") == "assistant" and current.get("tool_calls"):
                while index < len(messages) and messages[index].get("role") == "tool":
                    group.append(dict(messages[index]))
                    index += 1
                required_ids = {str(call.get("id") or "") for call in current.get("tool_calls") or []}
                actual_ids = {str(item.get("tool_call_id") or "") for item in group[1:]}
                if required_ids and required_ids.issubset(actual_ids):
                    groups.append(group)
                # Incomplete tool-call groups are omitted instead of emitting an
                # invalid provider transcript.
            elif current.get("role") != "tool":
                # A standalone tool result is invalid without its assistant call.
                groups.append(group)
        return groups

    @staticmethod
    def _merge_tool_results(
        recent_messages: Sequence[Mapping[str, Any]],
        tool_results: Sequence[Mapping[str, Any]],
    ) -> tuple[list[Message], int]:
        """Place detached tool results immediately after their assistant call."""

        recent = [dict(message) for message in recent_messages]
        existing_ids = {
            str(message.get("tool_call_id"))
            for message in recent
            if message.get("role") == "tool" and message.get("tool_call_id")
        }
        waiting: dict[str, list[Message]] = {}
        dropped = 0
        for raw in tool_results:
            result = dict(raw)
            call_id = str(result.get("tool_call_id") or "")
            if call_id and call_id not in existing_ids:
                waiting.setdefault(call_id, []).append(result)
            else:
                dropped += 1

        merged: list[Message] = []
        for message in recent:
            merged.append(message)
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                call_id = str(call.get("id") or "")
                merged.extend(waiting.pop(call_id, []))
        dropped += sum(len(results) for results in waiting.values())
        return merged, dropped

    @staticmethod
    def _group_tokens(group: Sequence[Message]) -> int:
        return sum(message_tokens(message) for message in group)

    def _shrink_group(self, group: list[Message], token_budget: int) -> list[Message]:
        """Trim content fields without breaking provider tool-call pairing."""

        shrunk = [dict(message) for message in group]
        marker = "\n[较早内容已压缩]"
        while self._group_tokens(shrunk) > token_budget:
            candidates = [
                (len(str(message.get("content", ""))), index)
                for index, message in enumerate(shrunk)
                if message.get("content")
            ]
            if not candidates:
                break  # Tool-call arguments alone can be irreducible.
            length, index = max(candidates)
            content = str(shrunk[index].get("content", ""))
            if length <= len(marker) + 8:
                shrunk[index]["content"] = ""
            else:
                shrunk[index]["content"] = content[: max(0, length // 2)] + marker
        return shrunk

    def trim_runtime_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> tuple[list[Message], int]:
        """Re-apply the context budget between tool-loop iterations.

        Leading system layers are retained. Conversation history is selected as
        newest complete groups so an assistant tool call is never detached from
        its tool response, which OpenAI-compatible providers reject.
        """

        budget = max_tokens or self.max_tokens
        normalized = [dict(message) for message in messages]
        system_candidates: list[Message] = []
        cursor = 0
        while cursor < len(normalized) and normalized[cursor].get("role") == "system":
            system_candidates.append(normalized[cursor])
            cursor += 1
        system: list[Message] = []
        system_tokens = 0
        for candidate in system_candidates:
            fitted, was_cut = self._fit_message(candidate, budget - system_tokens)
            if fitted is None:
                break
            system.append(fitted)
            system_tokens += message_tokens(fitted)
            if was_cut:
                break
        available = max(0, budget - system_tokens)
        groups = self._conversation_groups(normalized[cursor:])
        selected: list[list[Message]] = []
        used = 0
        for group in reversed(groups):
            cost = self._group_tokens(group)
            if cost + used <= available:
                selected.append(group)
                used += cost
                continue
            if not selected and available > 0:
                compacted = self._shrink_group(group, available)
                if self._group_tokens(compacted) <= available:
                    selected.append(compacted)
                    used += self._group_tokens(compacted)
            # Once an older group does not fit, even older groups are omitted.
            break
        selected.reverse()
        flattened = [message for group in selected for message in group]
        omitted = max(0, len(normalized) - len(system) - len(flattened))
        return [*system, *flattened], omitted
