from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent import (
    AgentRuntime,
    CompletionDecision,
    decide_deterministic_completion,
    ModelTurn,
    ModelToolCall,
    RuntimeConfig,
    verify_deterministic_completion,
)
from src.tools import create_default_registry


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
        CompletionDecision(False, "确定性轨迹不完整", {"stage": "deterministic", "passed": False}),
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
        if item.get("role") == "user" and "确定性验收拒绝" in str(item.get("content") or "")
    ]
    assert len(feedback_messages) == 1
    assert "确定性轨迹不完整" in feedback_messages[0]["content"]
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


def test_production_completion_decision_is_deterministic_and_has_no_model_usage() -> None:
    decision = decide_deterministic_completion({
        "output": "One fact from the conversation is that 1 + 1 was answered as 2.",
        "messages": [{
            "role": "assistant",
            "content": "One fact from the conversation is that 1 + 1 was answered as 2.",
        }],
        "tool_calls": 0,
        "attempt": 1,
    })

    assert decision.accepted
    assert decision.report["stage"] == "deterministic"
    assert decision.report["deterministic"]["passed"] is True
    assert decision.usage == {}


@pytest.mark.asyncio
async def test_simple_runtime_answer_uses_only_the_main_model_call(tmp_path: Path) -> None:
    calls = 0

    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal calls
        calls += 1
        return ModelTurn(content="1 + 1 = 2.", usage={"request_count": 1})

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        completion_verifier=decide_deterministic_completion,
    )
    outcome = await runtime.run(
        system_prompt="safe",
        recent_messages=[{"role": "user", "content": "What is 1 + 1?"}],
    )

    assert outcome.status == "completed"
    assert outcome.output == "1 + 1 = 2."
    assert calls == 1
    assert outcome.usage["request_count"] == 1
