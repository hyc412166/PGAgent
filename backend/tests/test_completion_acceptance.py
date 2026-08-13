from __future__ import annotations

import json
import asyncio
from pathlib import Path

import pytest

from app.runtime import (
    AgentRuntime,
    CompletionDecision,
    ModelTurn,
    ModelToolCall,
    RuntimeConfig,
    verify_deterministic_completion,
)
from app.services.completion_evaluator import build_completion_verifier
from app.tools import create_default_registry


SCENARIO_PATH = Path(__file__).with_name("acceptance_scenarios.json")


def _tool_call(call_id: str = "call-1", name: str = "read") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }],
    }


def _tool_result(call_id: str = "call-1", name: str = "read") -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps({"tool_name": name, "ok": True, "content": "evidence"}),
    }


def test_deterministic_acceptance_manifest_executes_all_scenarios() -> None:
    scenarios = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    assert [item["name"] for item in scenarios] == [
        "plain_text_pass",
        "tool_roundtrip_pass",
        "orphan_result_reject",
        "unresolved_call_reject",
        "count_mismatch_reject",
        "empty_output_reject",
    ]
    for scenario in scenarios:
        inputs = {key: scenario[key] for key in ("output", "messages", "declared_tool_calls")}
        report = verify_deterministic_completion(**inputs)
        assert report.passed is scenario["expected_pass"], (scenario["name"], report.to_dict())


@pytest.mark.asyncio
async def test_runtime_rejected_candidate_is_revised_and_only_accepted_answer_is_final(tmp_path: Path) -> None:
    model_outputs = iter(["unverified answer", "verified answer"])
    decisions = iter([
        CompletionDecision(False, "缺少测试证据", {"stage": "semantic", "passed": False}),
        CompletionDecision(True, "all criteria passed", {"stage": "complete", "passed": True}),
    ])
    observed_model_messages: list[list[dict]] = []
    streamed: list[dict] = []
    verified_candidates: list[dict] = []

    async def model_call(**kwargs) -> ModelTurn:
        observed_model_messages.append(list(kwargs["messages"]))
        content = next(model_outputs)
        await kwargs["on_delta"](content)
        return ModelTurn(content=content)

    async def verifier(_candidate: dict) -> CompletionDecision:
        verified_candidates.append(_candidate)
        return next(decisions)

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        completion_verifier=verifier,
        stream_sink=streamed.append,
        config=RuntimeConfig(max_completion_verification_attempts=3),
    )
    outcome = await runtime.run(
        system_prompt="safe",
        recent_messages=[
            _tool_call("historical-call"),
            _tool_result("historical-call"),
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "finish and verify"},
        ],
    )

    assert outcome.status == "completed"
    assert outcome.output == "verified answer"
    assert [event["type"] for event in outcome.events].count("completion_verification_rejected") == 1
    assert [event["type"] for event in outcome.events].count("completion_verification_passed") == 1
    feedback_messages = [
        item for item in observed_model_messages[1]
        if item.get("role") == "user" and "独立验收器拒绝" in str(item.get("content") or "")
    ]
    assert len(feedback_messages) == 1
    assert "缺少测试证据" in feedback_messages[0]["content"]
    assert outcome.transcript_delta == [{"role": "assistant", "content": "verified answer"}]
    assert outcome.acceptance_report["passed"] is True
    assert [item["delta"] for item in streamed if item["type"] == "assistant_delta"] == ["verified answer"]
    assert verified_candidates[0]["messages"] == [{"role": "assistant", "content": "unverified answer"}]
    assert all("unverified answer" not in str(item.get("content") or "") for item in outcome.messages)
    assert all("内部验收反馈" not in str(item.get("content") or "") for item in outcome.messages)
    rejected_events = [item for item in outcome.events if item["type"] == "completion_verification_rejected"]
    assert all("reason" not in item and "report" not in item for item in rejected_events)


@pytest.mark.asyncio
async def test_runtime_fails_closed_after_verification_attempt_limit(tmp_path: Path) -> None:
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(content="same unsupported claim")

    async def verifier(_candidate: dict) -> CompletionDecision:
        return CompletionDecision(False, "evidence missing", {"passed": False})

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        completion_verifier=verifier,
        config=RuntimeConfig(max_completion_verification_attempts=2),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[{"role": "user", "content": "do it"}])

    assert outcome.status == "stopped"
    assert outcome.stop_reason == "acceptance_failed"
    assert outcome.output is None
    assert not outcome.transcript_delta
    assert [event["type"] for event in outcome.events].count("completion_verification_rejected") == 2
    assert all("same unsupported claim" not in str(item.get("content") or "") for item in outcome.messages)
    assert "same unsupported claim" not in str(outcome.error)


@pytest.mark.asyncio
async def test_runtime_verifies_only_explicit_current_run_tool_trace(tmp_path: Path) -> None:
    turns = iter([
        ModelTurn(tool_calls=[ModelToolCall("current-call", "list_files", {"path": "."})]),
        ModelTurn(content="current answer"),
    ])
    captured: list[dict] = []

    async def model_call(**_kwargs) -> ModelTurn:
        return next(turns)

    async def verifier(candidate: dict) -> CompletionDecision:
        captured.append(candidate)
        report = verify_deterministic_completion(
            output=candidate["output"],
            messages=candidate["messages"],
            declared_tool_calls=candidate["tool_calls"],
        )
        return CompletionDecision(report.passed, "checked", report.to_dict())

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=["list_files"]),
        completion_verifier=verifier,
    )
    outcome = await runtime.run(
        system_prompt="safe",
        recent_messages=[_tool_call("historical-call"), _tool_result("historical-call"), {"role": "user", "content": "new"}],
    )

    assert outcome.status == "completed"
    encoded = json.dumps(captured[0]["messages"], ensure_ascii=False)
    assert "current-call" in encoded
    assert "historical-call" not in encoded
    assert outcome.verification_trace[0]["tool_calls"][0]["id"] == "current-call"


def test_deterministic_acceptance_rejects_tool_name_mismatch() -> None:
    result = _tool_result(name="write_file")
    report = verify_deterministic_completion(
        output="done",
        messages=[_tool_call(name="read"), result, {"role": "assistant", "content": "done"}],
        declared_tool_calls=1,
    )
    assert not report.passed
    assert next(item for item in report.checks if item.name == "tool_result_names_match_calls").passed is False


@pytest.mark.asyncio
async def test_independent_evaluator_passes_only_strict_structured_result(tmp_path: Path) -> None:
    calls: list[dict] = []

    async def evaluator_call(**kwargs) -> ModelTurn:
        calls.append(kwargs)
        return ModelTurn(
            content=json.dumps({
                "passed": True,
                "score": 0.94,
                "summary": "requirements and evidence match",
                "criteria": [{"criterion": "answer provided", "passed": True, "evidence": "candidate is non-empty"}],
                "feedback": "",
                "confidence": 0.9,
            }),
            usage={"request_count": 1, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    verifier = build_completion_verifier(
        evaluator_call=evaluator_call,
        original_task="answer the question with evidence",
        score_threshold=0.8,
    )
    decision = await verifier({
        "output": "done with evidence",
        "messages": [{"role": "assistant", "content": "done with evidence"}],
        "tool_calls": 0,
        "attempt": 1,
    })

    assert decision.accepted
    assert decision.report["deterministic"]["passed"] is True
    assert decision.report["semantic"]["score"] == 0.94
    assert decision.usage["request_count"] == 1
    assert calls[0]["tools"] == []
    assert calls[0]["mode"] == "evaluation"
    evaluation_payload = json.loads(calls[0]["messages"][1]["content"])
    assert "workspace_evidence" not in evaluation_payload


@pytest.mark.asyncio
async def test_evaluator_ignores_tool_calls_from_prior_session_history(tmp_path: Path) -> None:
    async def evaluator_call(**_kwargs) -> ModelTurn:
        return ModelTurn(content=json.dumps({
            "passed": True,
            "score": 1,
            "summary": "current task passed",
            "criteria": [{"criterion": "current reply", "passed": True, "evidence": "current candidate"}],
            "feedback": "",
            "confidence": 1,
        }))

    verifier = build_completion_verifier(
        evaluator_call=evaluator_call,
        original_task="new follow-up task",
        score_threshold=0.8,
    )
    # The runtime deliberately supplies only the current Run trace. Historical
    # calls may exist in provider context, but must not affect this count.
    decision = await verifier({
        "output": "new answer",
        "messages": [{"role": "assistant", "content": "new answer"}],
        "tool_calls": 0,
    })
    assert decision.accepted


@pytest.mark.asyncio
async def test_independent_evaluator_rejects_missing_criteria_and_invalid_json(tmp_path: Path) -> None:
    responses = iter([
        ModelTurn(content=json.dumps({"passed": True, "score": 1, "criteria": []})),
        ModelTurn(content="not json"),
    ])

    async def evaluator_call(**_kwargs) -> ModelTurn:
        return next(responses)

    verifier = build_completion_verifier(
        evaluator_call=evaluator_call,
        original_task="prove completion",
        score_threshold=0.8,
    )
    candidate = {
        "output": "claim",
        "messages": [{"role": "assistant", "content": "claim"}],
        "tool_calls": 0,
    }
    missing_criteria = await verifier(candidate)
    invalid_json = await verifier(candidate)

    assert not missing_criteria.accepted
    assert not missing_criteria.report["semantic"]["passed"]
    assert not invalid_json.accepted
    assert invalid_json.report["protocol_error"] == "invalid_json"


@pytest.mark.asyncio
async def test_independent_evaluator_timeout_fails_closed(tmp_path: Path) -> None:
    async def evaluator_call(**_kwargs) -> ModelTurn:
        await asyncio.sleep(1)
        return ModelTurn(content="{}")

    verifier = build_completion_verifier(
        evaluator_call=evaluator_call,
        original_task="prove completion",
        score_threshold=0.8,
        timeout_seconds=0.01,
    )
    decision = await verifier({
        "output": "claim",
        "messages": [{"role": "assistant", "content": "claim"}],
        "tool_calls": 0,
    })
    assert not decision.accepted
    assert decision.report["protocol_error"] == "timeout"


def test_synchronous_evaluator_is_rejected_because_it_cannot_be_cancelled(tmp_path: Path) -> None:
    def evaluator_call(**_kwargs) -> ModelTurn:
        return ModelTurn(content="{}")

    with pytest.raises(TypeError, match="must be async"):
        build_completion_verifier(
            evaluator_call=evaluator_call,
            original_task="prove completion",
            score_threshold=0.8,
        )
