"""Token estimation and provider tool-protocol validation.

Prompt assembly and full transcript replacement live in ``context_service``.
This module deliberately has no task anchors, message trimming, micro-
compaction, summaries, memories, epochs, or dynamic preambles.
"""
# 文件职责：负责模型上下文组装、窗口预算与压缩中的 window 子模块。
# 逻辑关系：上层通过 context/window.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any, Mapping, Sequence


# 变量说明：Message 表示当前消息。
Message = dict[str, Any]
# 变量说明：DEFAULT_CONTEXT_LIMIT_TOKENS 表示当前流程使用的 DEFAULT_CONTEXT_LIMIT_TOKENS 集合。
DEFAULT_CONTEXT_LIMIT_TOKENS = 200_000
# 变量说明：DEFAULT_COMPACT_THRESHOLD_TOKENS 表示当前流程使用的 DEFAULT_COMPACT_THRESHOLD_TOKENS 集合。
DEFAULT_COMPACT_THRESHOLD_TOKENS = 180_000


# 函数职责：完成 estimate_tokens 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def estimate_tokens(value: str | Mapping[str, Any] | Sequence[Any]) -> int:
    """Return a conservative provider-neutral estimate for input budgeting."""

    if not isinstance(value, str):
        # 变量说明：value 表示当前字段或计算值。
        value = json.dumps(value, ensure_ascii=False, default=str)
    # 变量说明：non_ascii 表示当前步骤使用的 non_ascii 值。
    non_ascii = sum(1 for character in value if ord(character) > 127)
    # 变量说明：ascii_count 表示ascii 的数量。
    ascii_count = len(value) - non_ascii
    return max(1, (non_ascii * 2) + math.ceil(ascii_count / 4))


# 函数职责：完成 message_tokens 对应的业务处理。
# 参数关系：message 表示当前消息。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def message_tokens(message: Mapping[str, Any]) -> int:
    # 变量说明：content 表示待处理或返回的正文内容。
    content = message.get("content")
    # 变量说明：image_tokens 表示当前流程使用的 image_tokens 集合。
    image_tokens = 0
    if isinstance(content, list):
        # 变量说明：image_tokens 表示当前流程使用的 image_tokens 集合。
        image_tokens = 1_200 * sum(
            1 for item in content
            if isinstance(item, Mapping) and item.get("type") == "pgagent_image_ref"
        )
    return 4 + estimate_tokens(dict(message)) + image_tokens


# 类职责：协调 ContextManager 负责的业务流程与依赖。
class ContextManager:
    """Own the context limit and remove only provider-invalid tool groups."""

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：max_tokens 表示当前流程使用的 max_tokens 集合；_ignored 表示当前步骤使用的 _ignored 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self, *, max_tokens: int = DEFAULT_CONTEXT_LIMIT_TOKENS, **_ignored: Any) -> None:
        if max_tokens < 256:
            raise ValueError("max_tokens must be at least 256")
        # 变量说明：max_tokens 表示当前流程使用的 max_tokens 集合。
        self.max_tokens = int(max_tokens)

    # 函数职责：完成 conversation_groups 对应的业务处理。
    # 参数关系：messages 表示发送给模型或客户端的消息序列。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _conversation_groups(messages: Sequence[Mapping[str, Any]]) -> list[list[Message]]:
        # 变量说明：groups 表示当前流程使用的 groups 集合。
        groups: list[list[Message]] = []
        # 变量说明：index 表示当前元素的位置索引。
        index = 0
        while index < len(messages):
            # 变量说明：current 表示当前步骤使用的 current 值。
            current = dict(messages[index])
            index += 1
            if current.get("role") == "tool":
                # A detached tool result is invalid at the provider boundary.
                continue
            # 变量说明：calls 表示当前流程使用的 calls 集合。
            calls = current.get("tool_calls")
            if current.get("role") != "assistant" or not isinstance(calls, list) or not calls:
                groups.append([current])
                continue

            # 变量说明：results 表示批量处理结果集合。
            results: list[Message] = []
            while index < len(messages) and messages[index].get("role") == "tool":
                results.append(dict(messages[index]))
                index += 1
            # 变量说明：required 表示当前步骤使用的 required 值。
            required = Counter(
                str(call.get("id") or "")
                for call in calls
                if isinstance(call, Mapping)
            )
            # 变量说明：actual 表示当前步骤使用的 actual 值。
            actual = Counter(str(result.get("tool_call_id") or "") for result in results)
            if (
                required
                and required == actual
                and "" not in required
                and all(count == 1 for count in required.values())
            ):
                groups.append([current, *results])
        return groups

    # 函数职责：完成 repair_provider_messages 对应的业务处理。
    # 参数关系：messages 表示发送给模型或客户端的消息序列。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def repair_provider_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> tuple[list[Message], dict[str, Any]]:
        """Drop corrupt historical tool batches without changing valid content."""

        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized = [dict(message) for message in messages]
        # 变量说明：cursor 表示当前步骤使用的 cursor 值。
        cursor = 0
        while cursor < len(normalized) and normalized[cursor].get("role") == "system":
            cursor += 1
        # 变量说明：repaired 表示当前步骤使用的 repaired 值。
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

        # 函数职责：完成 identity 对应的业务处理。
        # 参数关系：message 表示当前消息。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def identity(message: Mapping[str, Any]) -> str:
            return json.dumps(dict(message), ensure_ascii=False, sort_keys=True, default=str)

        # 变量说明：retained 表示当前步骤使用的 retained 值。
        retained = Counter(identity(message) for message in repaired)
        # 变量说明：removed 表示当前步骤使用的 removed 值。
        removed: list[Message] = []
        for message in normalized:
            # 变量说明：key 表示用于查找或映射的键。
            key = identity(message)
            if retained[key] > 0:
                retained[key] -= 1
            else:
                removed.append(message)
        # 变量说明：affected 表示当前步骤使用的 affected 值。
        affected: set[str] = set()
        for message in removed:
            # 变量说明：call_id 表示call 对象的唯一标识。
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
