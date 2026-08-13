"""Independent, fail-closed completion evaluator for main-agent candidates."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from typing import Any, Awaitable, Callable, Mapping, Sequence

from app.runtime import (
    CompletionDecision,
    ModelTurn,
    verify_deterministic_completion,
)


EvaluatorCall = Callable[..., Any | Awaitable[Any]]


_EVALUATOR_SYSTEM_PROMPT = """你是独立的任务验收器，不是执行任务的主 Agent。
你的唯一职责是根据原始任务、完整运行证据和候选答复进行严格验收。
不得因为候选答复声称“已完成”就判定通过；缺少可验证证据时必须失败。
逐项覆盖用户明确要求、可合理推导的验收标准、失败的工具调用、未完成事项和测试证据。
你没有工具，也不能修改任何内容。只输出一个 JSON 对象，不要输出 Markdown：
{
  "passed": true或false,
  "score": 0到1之间的数字,
  "summary": "简短结论",
  "criteria": [
    {"criterion": "验收项", "passed": true或false, "evidence": "轨迹中的具体证据"}
  ],
  "feedback": "失败时给主 Agent 的可执行修正建议；通过时为空字符串",
  "confidence": 0到1之间的数字
}
只有所有关键验收项都有证据且没有相互矛盾时，passed 才能为 true。"""


def _bounded(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[:limit]}\n...[truncated {len(text) - limit} chars]"


def _trace_evidence(messages: Sequence[Mapping[str, Any]], *, limit: int = 30_000) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "").lower()
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            calls: list[dict[str, str]] = []
            for raw in message["tool_calls"]:
                if not isinstance(raw, Mapping):
                    continue
                function = raw.get("function")
                raw_arguments = function.get("arguments") if isinstance(function, Mapping) else ""
                try:
                    parsed_arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except json.JSONDecodeError:
                    parsed_arguments = {"_raw_chars": len(raw_arguments)}
                if isinstance(parsed_arguments, Mapping):
                    safe_arguments = {
                        str(key): (
                            {"chars": len(str(value or ""))}
                            if any(marker in str(key).casefold() for marker in (
                                "content", "old_string", "new_string", "token", "secret", "password", "cookie", "api_key"
                            ))
                            else _bounded(value, 500)
                        )
                        for key, value in parsed_arguments.items()
                    }
                else:
                    safe_arguments = {"_raw_chars": len(str(raw_arguments or ""))}
                calls.append({
                    "id": str(raw.get("id") or ""),
                    "name": str(function.get("name") or "") if isinstance(function, Mapping) else "",
                    "arguments": safe_arguments,
                })
            evidence.append({"role": "assistant", "tool_calls": calls})
        elif role == "tool":
            evidence.append({
                "role": "tool",
                "name": str(message.get("name") or ""),
                "tool_call_id": str(message.get("tool_call_id") or ""),
                "result": _bounded(message.get("content"), 3_000),
            })
    encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return evidence
    # Remove middle items until the serialized payload really fits. This keeps
    # the run's first actions and freshest observations without claiming the
    # bounded trace is complete.
    marker = {"truncated": True, "original_chars": len(encoded), "removed_items": len(evidence)}
    marker_chars = len(json.dumps(marker, ensure_ascii=False, separators=(",", ":")))
    remaining = max(0, limit - marker_chars - 4)
    head: list[dict[str, Any]] = []
    tail: list[dict[str, Any]] = []
    left, right = 0, len(evidence) - 1
    take_head = True
    while left <= right:
        index = left if take_head else right
        item = evidence[index]
        item_chars = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) + 1
        if item_chars > remaining:
            break
        (head if take_head else tail).append(item)
        remaining -= item_chars
        if take_head:
            left += 1
        else:
            right -= 1
        take_head = not take_head
    tail.reverse()
    marker["removed_items"] = len(evidence) - len(head) - len(tail)
    bounded = [*head, marker, *tail]
    return bounded


def _extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("evaluator did not return a JSON object")
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("evaluator JSON must be an object")
    return value


def _safe_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return min(1.0, max(0.0, score))


def _normalize_evaluation(payload: Mapping[str, Any], *, threshold: float) -> dict[str, Any]:
    criteria: list[dict[str, Any]] = []
    raw_criteria = payload.get("criteria")
    if isinstance(raw_criteria, list):
        for item in raw_criteria[:50]:
            if not isinstance(item, Mapping):
                continue
            criteria.append({
                "criterion": _bounded(item.get("criterion"), 1_000),
                "passed": item.get("passed") is True,
                "evidence": _bounded(item.get("evidence"), 2_000),
            })
    score = _safe_score(payload.get("score"))
    model_passed = payload.get("passed") is True
    all_criteria_passed = bool(criteria) and all(item["passed"] for item in criteria)
    passed = model_passed and score >= threshold and all_criteria_passed
    return {
        "passed": passed,
        "model_passed": model_passed,
        "score": score,
        "threshold": threshold,
        "summary": _bounded(payload.get("summary"), 2_000),
        "criteria": criteria,
        "feedback": _bounded(payload.get("feedback"), 4_000),
        "confidence": _safe_score(payload.get("confidence")),
    }


def build_completion_verifier(
    *,
    evaluator_call: EvaluatorCall,
    original_task: str,
    score_threshold: float,
    timeout_seconds: float = 90.0,
) -> Callable[[dict[str, Any]], Awaitable[CompletionDecision]]:
    """Create one isolated verifier callback for a run."""

    threshold = min(1.0, max(0.0, float(score_threshold)))
    if not inspect.iscoroutinefunction(evaluator_call):
        raise TypeError("completion evaluator must be async so timeout cancellation is enforceable")

    async def verify(candidate: dict[str, Any]) -> CompletionDecision:
        messages = [item for item in candidate.get("messages") or [] if isinstance(item, Mapping)]
        deterministic = verify_deterministic_completion(
            output=candidate.get("output"),
            messages=messages,
            declared_tool_calls=int(candidate.get("tool_calls") or 0),
            pending_approval=candidate.get("pending_approval"),
        )
        deterministic_payload = deterministic.to_dict()
        if not deterministic.passed:
            failed = [check.detail for check in deterministic.checks if not check.passed]
            return CompletionDecision(
                accepted=False,
                reason="确定性运行链路未通过：" + "；".join(failed),
                report={"stage": "deterministic", "deterministic": deterministic_payload},
            )

        evaluation_input = {
            "original_task": _bounded(original_task, 20_000),
            "candidate_answer": _bounded(candidate.get("output"), 20_000),
            "deterministic_report": deterministic_payload,
            "tool_trace": _trace_evidence(messages),
            # Evidence is intentionally limited to this run's explicit tool
            # trace. Reading the live worktree would mix in unrelated edits and
            # expand the provider privacy boundary.
            "verification_attempt": int(candidate.get("attempt") or 1),
        }
        evaluator_kwargs = {
            "messages": [
                {"role": "system", "content": _EVALUATOR_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(evaluation_input, ensure_ascii=False, separators=(",", ":"))},
            ],
            "tools": [],
            "mode": "evaluation",
        }

        async def invoke_evaluator() -> Any:
            response = evaluator_call(**evaluator_kwargs)
            return await response if inspect.isawaitable(response) else response

        try:
            raw_response = await asyncio.wait_for(
                invoke_evaluator(),
                timeout=max(0.1, float(timeout_seconds)),
            )
        except asyncio.TimeoutError:
            return CompletionDecision(
                accepted=False,
                reason="独立验收器调用超时",
                report={"stage": "evaluator", "deterministic": deterministic_payload, "protocol_error": "timeout"},
            )
        turn = ModelTurn.from_response(raw_response)
        if turn.tool_calls:
            return CompletionDecision(
                accepted=False,
                reason="独立验收器违反只读协议并请求了工具调用",
                report={"stage": "evaluator", "deterministic": deterministic_payload, "protocol_error": "tool_call"},
                usage=turn.usage,
            )
        try:
            semantic = _normalize_evaluation(_extract_json_object(turn.content), threshold=threshold)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return CompletionDecision(
                accepted=False,
                reason=f"独立验收器返回格式无效：{exc}",
                report={"stage": "evaluator", "deterministic": deterministic_payload, "protocol_error": "invalid_json"},
                usage=turn.usage,
            )
        report = {"stage": "complete", "deterministic": deterministic_payload, "semantic": semantic}
        if semantic["passed"]:
            return CompletionDecision(accepted=True, reason=semantic["summary"] or "验收通过", report=report, usage=turn.usage)
        feedback = semantic["feedback"] or semantic["summary"] or "关键验收项缺少证据或未通过"
        return CompletionDecision(accepted=False, reason=feedback, report=report, usage=turn.usage)

    return verify
