"""Provider-facing memory index and accepted-response citation protocol."""
# 文件职责：负责长期记忆提取、合并、检索与偏好中的 protocol 子模块。
# 逻辑关系：上层通过 memory/protocol.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence


# 变量说明：MEMORY_CITATION_OPEN 表示当前步骤使用的 MEMORY_CITATION_OPEN 值。
MEMORY_CITATION_OPEN = "<pgagent-memory-citation>"
# 变量说明：MEMORY_CITATION_CLOSE 表示当前步骤使用的 MEMORY_CITATION_CLOSE 值。
MEMORY_CITATION_CLOSE = "</pgagent-memory-citation>"
# 变量说明：_CITATION_RE 表示当前步骤使用的 _CITATION_RE 值。
_CITATION_RE = re.compile(
    rf"\s*{re.escape(MEMORY_CITATION_OPEN)}(.*?){re.escape(MEMORY_CITATION_CLOSE)}\s*",
    re.DOTALL,
)


# 变量说明：MEMORY_ROUTER_INSTRUCTIONS 表示当前流程使用的 MEMORY_ROUTER_INSTRUCTIONS 集合。
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


# 函数职责：完成 memory_system_message 对应的业务处理。
# 参数关系：index_text 表示index 的文本表示。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def memory_system_message(index_text: str) -> dict[str, Any]:
    """Build the late system message used by root and delegated runs."""

    return {
        "role": "system",
        "content": f"{MEMORY_ROUTER_INSTRUCTIONS}\n\n# Available memory index\n\n{index_text.strip()}",
    }


# 函数职责：完成 split_memory_citation 对应的业务处理。
# 参数关系：text 表示当前步骤使用的 text 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def split_memory_citation(text: str) -> tuple[str, dict[str, Any]]:
    """Remove and validate one trailing citation block from a candidate reply."""

    # 变量说明：source 表示当前步骤使用的 source 值。
    source = str(text or "")
    # 变量说明：matches 表示当前流程使用的 matches 集合。
    matches = list(_CITATION_RE.finditer(source))
    if not matches:
        return source, {}
    # 变量说明：match 表示当前步骤使用的 match 值。
    match = matches[-1]
    if source[match.end() :].strip():
        return source, {}
    # 变量说明：visible 表示当前步骤使用的 visible 值。
    visible = source[: matches[0].start()].rstrip()
    if len(matches) != 1:
        return visible, {}
    try:
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return visible, {}
    if not isinstance(payload, Mapping):
        return visible, {}

    # 函数职责：完成 ids 对应的业务处理。
    # 参数关系：name 表示当前对象名称。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def ids(name: str) -> list[str]:
        # 变量说明：values 表示当前流程使用的 values 集合。
        values = payload.get(name)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            return []
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))[:20]

    # 变量说明：citation 表示当前步骤使用的 citation 值。
    citation = {
        "memory_ids": ids("memory_ids"),
        "rollout_ids": ids("rollout_ids"),
        "skill_ids": ids("skill_ids"),
        "note": str(payload.get("note") or "").strip()[:500],
    }
    if not citation["memory_ids"] and not citation["rollout_ids"] and not citation["skill_ids"]:
        # 变量说明：citation 表示当前步骤使用的 citation 值。
        citation = {}
    return visible, citation
