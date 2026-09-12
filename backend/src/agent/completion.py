"""Legacy deterministic completion helpers.

The default runtime no longer installs or invokes these checks.  They remain
available for explicit workflow policies and for reading historical reports.
"""
# 文件职责：负责智能体状态机、模型行动、工具观察与完成判定。
# 逻辑关系：本模块接收上层运行服务的输入，推进智能体执行后把状态、事件或结果返回调用层。

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence


# 类职责：封装 DeterministicCheck 的状态、依赖和领域行为。
# 协作关系：实例由运行服务或相邻节点创建，并在智能体步骤之间传递数据。
@dataclass(slots=True)
class DeterministicCheck:
    # 变量说明：name 表示当前步骤使用的 name 值。
    name: str
    # 变量说明：passed 表示当前步骤使用的 passed 值。
    passed: bool
    # 变量说明：detail 表示当前步骤使用的 detail 值。
    detail: str


# 类职责：封装 DeterministicAcceptanceReport 的状态、依赖和领域行为。
# 协作关系：实例由运行服务或相邻节点创建，并在智能体步骤之间传递数据。
@dataclass(slots=True)
class DeterministicAcceptanceReport:
    # 变量说明：passed 表示当前步骤使用的 passed 值。
    passed: bool
    # 变量说明：checks 表示checks 集合。
    checks: list[DeterministicCheck] = field(default_factory=list)
    # 变量说明：declared_tool_calls 表示declared_tool_calls 集合。
    declared_tool_calls: int = 0
    # 变量说明：observed_tool_calls 表示observed_tool_calls 集合。
    observed_tool_calls: int = 0
    # 变量说明：observed_tool_results 表示observed_tool_results 集合。
    observed_tool_results: int = 0

    # 函数职责：完成 to_dict 对应的智能体处理。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 类职责：封装 CompletionDecision 的状态、依赖和领域行为。
# 协作关系：实例由运行服务或相邻节点创建，并在智能体步骤之间传递数据。
@dataclass(slots=True)
class CompletionDecision:
    # 变量说明：accepted 表示当前步骤使用的 accepted 值。
    accepted: bool
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str
    # 变量说明：report 表示当前步骤使用的 report 值。
    report: dict[str, Any] = field(default_factory=dict)
    # 变量说明：usage 表示当前步骤使用的 usage 值。
    usage: dict[str, Any] = field(default_factory=dict)
    # 变量说明：defer_until_event 表示当前步骤使用的 defer_until_event 值。
    defer_until_event: bool = False


# 函数职责：完成 tool_result_ids 对应的智能体处理。
# 参数关系：messages 表示模型消息序列。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
def _tool_result_ids(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(message.get("tool_call_id") or "").strip()
        for message in messages
        if str(message.get("role") or "").lower() == "tool"
    ]


# 函数职责：完成 verify_deterministic_completion 对应的智能体处理。
# 参数关系：output 表示当前步骤使用的 output 值；messages 表示模型消息序列；declared_tool_calls 表示declared_tool_calls 集合；pending_approval 表示当前步骤使用的 pending_approval 值。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
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

    # 变量说明：checks 表示checks 集合。
    checks: list[DeterministicCheck] = []

    # 函数职责：完成 record 对应的智能体处理。
    # 参数关系：name 表示当前步骤使用的 name 值；passed 表示当前步骤使用的 passed 值；detail 表示当前步骤使用的 detail 值。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    def record(name: str, passed: bool, detail: str) -> None:
        checks.append(DeterministicCheck(name=name, passed=passed, detail=detail))

    # 变量说明：assistant_messages 表示assistant_messages 集合。
    assistant_messages = [
        message for message in messages
        if str(message.get("role") or "").lower() == "assistant"
    ]
    # 变量说明：call_ids 表示call_ids 集合。
    call_ids: list[str] = []
    # 变量说明：malformed_calls 表示malformed_calls 集合。
    malformed_calls = 0
    for message in assistant_messages:
        # 变量说明：raw_calls 表示raw_calls 集合。
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            malformed_calls += 1
            continue
        for call in raw_calls:
            if not isinstance(call, Mapping):
                malformed_calls += 1
                continue
            # 变量说明：call_id 表示call 对象标识。
            call_id = str(call.get("id") or "").strip()
            # 变量说明：function 表示当前步骤使用的 function 值。
            function = call.get("function")
            # 变量说明：name 表示当前步骤使用的 name 值。
            name = str(function.get("name") or "").strip() if isinstance(function, Mapping) else ""
            if not call_id or not name:
                malformed_calls += 1
            call_ids.append(call_id)

    # 变量说明：result_ids 表示result_ids 集合。
    result_ids = _tool_result_ids(messages)
    # 变量说明：unique_ids 表示unique_ids 集合。
    unique_ids = {item for item in call_ids if item}
    # 变量说明：duplicate_calls 表示duplicate_calls 集合。
    duplicate_calls = len(unique_ids) != len(call_ids)
    record(
        "well_formed_unique_tool_calls",
        malformed_calls == 0 and not duplicate_calls,
        f"工具调用={len(call_ids)}，格式错误={malformed_calls}，重复 ID={duplicate_calls}",
    )

    # 变量说明：result_counts 表示result_counts 集合。
    result_counts = {call_id: result_ids.count(call_id) for call_id in set(result_ids)}
    # 变量说明：unresolved 表示当前步骤使用的 unresolved 值。
    unresolved = sorted(call_id for call_id in unique_ids if result_counts.get(call_id, 0) != 1)
    # 变量说明：orphaned 表示当前步骤使用的 orphaned 值。
    orphaned = sorted(call_id for call_id in result_counts if call_id not in unique_ids)
    record(
        "tool_calls_fully_settled",
        not unresolved and not orphaned,
        f"未正确结算={unresolved or '无'}，孤立结果={orphaned or '无'}",
    )

    # 变量说明：declared_matches 表示declared_matches 集合。
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
    # 变量说明：call_names 表示call_names 集合。
    call_names: dict[str, str] = {}
    for message in assistant_messages:
        for call in message.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            # 变量说明：function 表示当前步骤使用的 function 值。
            function = call.get("function")
            if isinstance(function, Mapping):
                call_names[str(call.get("id") or "").strip()] = str(function.get("name") or "").strip()

    # 变量说明：invalid_tool_payloads 表示invalid_tool_payloads 集合。
    invalid_tool_payloads = 0
    # 变量说明：mismatched_tool_names 表示mismatched_tool_names 集合。
    mismatched_tool_names = 0
    for message in messages:
        if str(message.get("role") or "").lower() != "tool":
            continue
        try:
            # 变量说明：payload 表示当前步骤使用的 payload 值。
            payload = json.loads(str(message.get("content") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_tool_payloads += 1
            continue
        if not isinstance(payload, Mapping) or "ok" not in payload or "tool_name" not in payload:
            invalid_tool_payloads += 1
            continue
        # 变量说明：call_id 表示call 对象标识。
        call_id = str(message.get("tool_call_id") or "").strip()
        # 变量说明：declared_name 表示当前步骤使用的 declared_name 值。
        declared_name = call_names.get(call_id, "")
        # 变量说明：message_name 表示当前步骤使用的 message_name 值。
        message_name = str(message.get("name") or "").strip()
        # 变量说明：payload_name 表示当前步骤使用的 payload_name 值。
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


# 函数职责：完成 decide_deterministic_completion 对应的智能体处理。
# 参数关系：candidate 表示当前步骤使用的 candidate 值。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
def decide_deterministic_completion(candidate: Mapping[str, Any]) -> CompletionDecision:
    """Turn one runtime candidate into a deterministic completion decision."""

    # 变量说明：messages 表示模型消息序列。
    messages = [item for item in candidate.get("messages") or [] if isinstance(item, Mapping)]
    # 变量说明：report 表示当前步骤使用的 report 值。
    report = verify_deterministic_completion(
        output=candidate.get("output"),
        messages=messages,
        declared_tool_calls=int(candidate.get("tool_calls") or 0),
        pending_approval=candidate.get("pending_approval"),
    )
    # 变量说明：payload 表示当前步骤使用的 payload 值。
    payload = {
        "stage": "deterministic",
        "passed": report.passed,
        "deterministic": report.to_dict(),
    }
    if report.passed:
        return CompletionDecision(True, "确定性验收通过", payload)
    # 变量说明：failed 表示当前步骤使用的 failed 值。
    failed = [check.detail for check in report.checks if not check.passed]
    return CompletionDecision(False, "确定性运行链路未通过：" + "；".join(failed), payload)
