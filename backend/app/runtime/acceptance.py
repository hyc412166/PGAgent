"""Deterministic completion checks shared by production and parity tests.

The checks in this module deliberately avoid model judgement.  They prove that
the runtime produced a well-formed, fully settled tool transcript before a
candidate answer is allowed to reach the semantic evaluator.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(slots=True)
class DeterministicCheck:
    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class DeterministicAcceptanceReport:
    passed: bool
    checks: list[DeterministicCheck] = field(default_factory=list)
    declared_tool_calls: int = 0
    observed_tool_calls: int = 0
    observed_tool_results: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CompletionDecision:
    accepted: bool
    reason: str
    report: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)


def _tool_result_ids(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(message.get("tool_call_id") or "").strip()
        for message in messages
        if str(message.get("role") or "").lower() == "tool"
    ]


def verify_deterministic_completion(
    *,
    output: str | None,
    messages: Sequence[Mapping[str, Any]],
    declared_tool_calls: int,
    pending_approval: Mapping[str, Any] | None = None,
) -> DeterministicAcceptanceReport:
    """Validate terminal invariants without relying on an LLM.

    This is intentionally a protocol/chain validator, not a quality grader.
    Tool failures are permitted because an agent may recover from them; an
    unresolved or orphaned tool call is not.
    """

    checks: list[DeterministicCheck] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append(DeterministicCheck(name=name, passed=passed, detail=detail))

    candidate = str(output or "")
    record("non_empty_output", bool(candidate.strip()), "候选答复非空" if candidate.strip() else "候选答复为空")

    assistant_messages = [
        message for message in messages
        if str(message.get("role") or "").lower() == "assistant"
    ]
    final_matches = bool(assistant_messages) and str(assistant_messages[-1].get("content") or "") == candidate
    record(
        "final_assistant_matches_output",
        final_matches,
        "最后一条 assistant 消息与候选答复一致" if final_matches else "最后一条 assistant 消息与候选答复不一致",
    )

    call_ids: list[str] = []
    malformed_calls = 0
    for message in assistant_messages:
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            malformed_calls += 1
            continue
        for call in raw_calls:
            if not isinstance(call, Mapping):
                malformed_calls += 1
                continue
            call_id = str(call.get("id") or "").strip()
            function = call.get("function")
            name = str(function.get("name") or "").strip() if isinstance(function, Mapping) else ""
            if not call_id or not name:
                malformed_calls += 1
            call_ids.append(call_id)

    result_ids = _tool_result_ids(messages)
    unique_ids = {item for item in call_ids if item}
    duplicate_calls = len(unique_ids) != len(call_ids)
    record(
        "well_formed_unique_tool_calls",
        malformed_calls == 0 and not duplicate_calls,
        f"工具调用={len(call_ids)}，格式错误={malformed_calls}，重复 ID={duplicate_calls}",
    )

    result_counts = {call_id: result_ids.count(call_id) for call_id in set(result_ids)}
    unresolved = sorted(call_id for call_id in unique_ids if result_counts.get(call_id, 0) != 1)
    orphaned = sorted(call_id for call_id in result_counts if call_id not in unique_ids)
    record(
        "tool_calls_fully_settled",
        not unresolved and not orphaned,
        f"未正确结算={unresolved or '无'}，孤立结果={orphaned or '无'}",
    )

    declared_matches = max(0, int(declared_tool_calls or 0)) == len(call_ids)
    record(
        "tool_call_count_matches_trace",
        declared_matches,
        f"运行时计数={max(0, int(declared_tool_calls or 0))}，轨迹计数={len(call_ids)}",
    )
    record(
        "no_pending_approval",
        not pending_approval,
        "不存在待审批调用" if not pending_approval else "仍存在待审批调用",
    )

    # Ensure tool observations remain machine-readable.  This catches partial
    # stream assembly and persistence corruption while allowing legacy plain
    # text observations to be reported as a hard failure.
    call_names: dict[str, str] = {}
    for message in assistant_messages:
        for call in message.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if isinstance(function, Mapping):
                call_names[str(call.get("id") or "").strip()] = str(function.get("name") or "").strip()

    invalid_tool_payloads = 0
    mismatched_tool_names = 0
    for message in messages:
        if str(message.get("role") or "").lower() != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_tool_payloads += 1
            continue
        if not isinstance(payload, Mapping) or "ok" not in payload or "tool_name" not in payload:
            invalid_tool_payloads += 1
            continue
        call_id = str(message.get("tool_call_id") or "").strip()
        declared_name = call_names.get(call_id, "")
        message_name = str(message.get("name") or "").strip()
        payload_name = str(payload.get("tool_name") or "").strip()
        if not declared_name or message_name != declared_name or payload_name != declared_name:
            mismatched_tool_names += 1
    record(
        "tool_results_are_structured",
        invalid_tool_payloads == 0,
        f"不可解析工具结果={invalid_tool_payloads}",
    )
    record(
        "tool_result_names_match_calls",
        mismatched_tool_names == 0,
        f"tool name mismatches={mismatched_tool_names}",
    )

    return DeterministicAcceptanceReport(
        passed=all(check.passed for check in checks),
        checks=checks,
        declared_tool_calls=max(0, int(declared_tool_calls or 0)),
        observed_tool_calls=len(call_ids),
        observed_tool_results=len(result_ids),
    )
