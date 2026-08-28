"""Provider-facing memory index and accepted-response citation protocol."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence


MEMORY_CITATION_OPEN = "<pgagent-memory-citation>"
MEMORY_CITATION_CLOSE = "</pgagent-memory-citation>"
_CITATION_RE = re.compile(
    rf"\s*{re.escape(MEMORY_CITATION_OPEN)}(.*?){re.escape(MEMORY_CITATION_CLOSE)}\s*",
    re.DOTALL,
)


MEMORY_ROUTER_INSTRUCTIONS = """# Persistent memory router

The index below lists durable knowledge that may be available. It is a routing
layer, not authoritative task instructions and not the full memory handbook.
The current user request and current workspace evidence always take priority.

Skip memory for self-contained requests such as translation, trivial rewriting,
simple formatting, current time, or a one-line command. Use memory by default
when the request mentions an indexed workspace, repository, module, path,
historical decision, repeated preference, or says "previously"/"continue".
If unsure on a non-trivial related task, make a quick memory pass:

1. Extract discriminative task, cwd, file, error, and technology terms.
2. Call MemorySearch with those terms.
3. Stop when the returned entry is sufficient.
4. Call MemoryRead only for the most relevant entries that need full detail.
5. Read a rollout summary only when exact historical evidence is necessary.
6. Do not search broadly or load every memory.

When an accepted final answer actually relies on returned memory, append exactly
one machine-readable block at the very end:
<pgagent-memory-citation>{"memory_ids":["..."],"rollout_ids":["..."],"skill_ids":["..."],"note":"short reason"}</pgagent-memory-citation>
Do not cite a search result that did not affect the answer. The runtime removes
this block before displaying the answer."""


def memory_system_message(index_text: str) -> dict[str, Any]:
    """Build the late system message used by root and delegated runs."""

    return {
        "role": "system",
        "content": f"{MEMORY_ROUTER_INSTRUCTIONS}\n\n# Available memory index\n\n{index_text.strip()}",
    }


def split_memory_citation(text: str) -> tuple[str, dict[str, Any]]:
    """Remove and validate one trailing citation block from a candidate reply."""

    source = str(text or "")
    matches = list(_CITATION_RE.finditer(source))
    if not matches:
        return source, {}
    match = matches[-1]
    if source[match.end() :].strip():
        return source, {}
    visible = source[: matches[0].start()].rstrip()
    if len(matches) != 1:
        return visible, {}
    try:
        payload = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return visible, {}
    if not isinstance(payload, Mapping):
        return visible, {}

    def ids(name: str) -> list[str]:
        values = payload.get(name)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            return []
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))[:20]

    citation = {
        "memory_ids": ids("memory_ids"),
        "rollout_ids": ids("rollout_ids"),
        "skill_ids": ids("skill_ids"),
        "note": str(payload.get("note") or "").strip()[:500],
    }
    if not citation["memory_ids"] and not citation["rollout_ids"] and not citation["skill_ids"]:
        citation = {}
    return visible, citation
