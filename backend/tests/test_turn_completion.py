"""验证 response ledger 只依据结构化运行事实推进 Turn 生命周期。"""

from __future__ import annotations

import subprocess
import sys

import pytest

from src.agent.turn import LocalToolStatus, TurnLedger, TurnStatus
from src.model.output import (
    AssistantMessageItem,
    HostedToolItem,
    LocalToolCallItem,
    NormalizedModelResponse,
    OutputPhase,
    ResponseStatus,
)


def _response(response_id: str, *items) -> NormalizedModelResponse:
    return NormalizedModelResponse(
        response_id=response_id,
        items=list(items),
        status=ResponseStatus.COMPLETED,
    )


@pytest.mark.parametrize(
    "statement",
    ["import src.agent; import src.model", "import src.model; import src.agent"],
)
def test_agent_and_model_packages_import_in_either_order(statement: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "phase",
    [OutputPhase.COMMENTARY, OutputPhase.FINAL_ANSWER, OutputPhase.UNKNOWN],
)
def test_assistant_phase_does_not_decide_turn_completion(phase: OutputPhase) -> None:
    ledger = TurnLedger()

    ledger.accept_response(_response(
        "resp-1",
        AssistantMessageItem(
            response_id="resp-1",
            item_id="msg-1",
            output_index=0,
            content="可交付正文",
            phase=phase,
        ),
    ))

    decision = ledger.decision()
    assert decision.status is TurnStatus.COMPLETED
    assert decision.follow_up is False
    assert decision.assistant_text == "可交付正文"


def test_explicit_end_turn_false_requires_follow_up_without_reading_text() -> None:
    ledger = TurnLedger()
    ledger.accept_response(_response(
        "resp-1",
        AssistantMessageItem(
            response_id="resp-1",
            item_id="msg-1",
            content="这段文案本身不能决定是否结束",
            phase=OutputPhase.FINAL_ANSWER,
            end_turn=False,
        ),
    ))

    decision = ledger.decision()
    assert decision.status is TurnStatus.OBSERVING
    assert decision.follow_up is True
    assert decision.reason == "provider_requested_follow_up"


def test_local_tool_must_drain_before_follow_up_and_result_commits_once() -> None:
    ledger = TurnLedger()
    local = LocalToolCallItem(
        response_id="resp-tools",
        item_id="tool-item-1",
        output_index=0,
        call_id="call-1",
        tool_name="read",
        arguments={"path": "README.md"},
    )
    ledger.accept_response(_response("resp-tools", local))

    assert ledger.decision().status is TurnStatus.DRAINING_TOOLS
    assert ledger.local_call_count == 1
    assert ledger.local_result_count == 0
    assert ledger.take_local_calls() == [local]
    assert ledger.take_local_calls() == []
    assert ledger.local_status("call-1") is LocalToolStatus.SCHEDULED
    ledger.mark_local_running("call-1")
    assert ledger.local_status("call-1") is LocalToolStatus.RUNNING
    with pytest.raises(ValueError, match="tool name mismatch"):
        ledger.commit_local_result("call-1", tool_name="write")
    ledger.commit_local_result("call-1", tool_name="read")
    assert ledger.local_status("call-1") is LocalToolStatus.RESULT_COMMITTED
    with pytest.raises(ValueError, match="already committed"):
        ledger.commit_local_result("call-1", tool_name="read")

    decision = ledger.decision()
    assert decision.status is TurnStatus.OBSERVING
    assert decision.follow_up is True
    assert decision.reason == "local_tool_results"
    assert ledger.local_result_count == 1

    ledger.accept_response(_response(
        "resp-final",
        AssistantMessageItem(
            response_id="resp-final",
            item_id="msg-final",
            content="工具结果已处理",
        ),
    ))
    assert ledger.decision().status is TurnStatus.COMPLETED


def test_hosted_tools_are_counted_but_excluded_from_local_drain() -> None:
    ledger = TurnLedger()
    local = LocalToolCallItem(
        response_id="resp-mixed",
        item_id="local-item",
        output_index=0,
        call_id="local-call",
        tool_name="glob",
        arguments={"path": "."},
    )
    hosted = HostedToolItem(
        response_id="resp-mixed",
        item_id="hosted-item",
        output_index=1,
        call_id="hosted-call",
        tool_name="web_search",
    )
    ledger.accept_response(_response("resp-mixed", local, hosted))

    assert ledger.hosted_tool_count == 1
    assert ledger.local_call_count == 1
    assert ledger.take_local_calls() == [local]
    ledger.mark_local_running("local-call")
    ledger.commit_local_result("local-call", tool_name="glob")
    assert ledger.decision().reason == "local_tool_results"


def test_incomplete_response_does_not_release_completed_local_item() -> None:
    ledger = TurnLedger()
    ledger.accept_response(NormalizedModelResponse(
        response_id="resp-in-progress",
        status="in_progress",
        items=[LocalToolCallItem(
            response_id="resp-in-progress",
            item_id="tool-pending",
            call_id="call-pending",
            tool_name="apply_patch",
            arguments={"patch": "must not run"},
        )],
    ))

    decision = ledger.decision()
    assert decision.status is TurnStatus.FAILED
    assert decision.reason == "model_response_incomplete"
    assert ledger.take_local_calls() == []
    assert ledger.local_status("call-pending") is LocalToolStatus.SCHEDULED


def test_hosted_only_response_without_assistant_text_is_not_completed() -> None:
    ledger = TurnLedger()
    ledger.accept_response(_response(
        "resp-hosted",
        HostedToolItem(
            response_id="resp-hosted",
            item_id="hosted-only",
            call_id="hosted-call",
            tool_name="web_search",
        ),
    ))

    decision = ledger.decision()
    assert decision.status is TurnStatus.FAILED
    assert decision.reason == "empty_model_output"


def test_approval_pause_keeps_undispatched_calls_scheduled() -> None:
    ledger = TurnLedger()
    first = LocalToolCallItem(
        response_id="resp-batch",
        item_id="first",
        call_id="first",
        tool_name="apply_patch",
        arguments={"patch": "first"},
    )
    second = LocalToolCallItem(
        response_id="resp-batch",
        item_id="second",
        call_id="second",
        tool_name="apply_patch",
        arguments={"patch": "second"},
    )
    ledger.accept_response(_response("resp-batch", first, second))
    assert ledger.take_local_calls() == [first, second]

    ledger.mark_local_running("first")
    ledger.set_waiting(approval=True)

    assert ledger.local_status("first") is LocalToolStatus.RUNNING
    assert ledger.local_status("second") is LocalToolStatus.SCHEDULED
    assert ledger.decision().status is TurnStatus.AWAITING_APPROVAL


def test_duplicate_local_call_id_is_rejected_before_second_dispatch() -> None:
    ledger = TurnLedger()
    first = LocalToolCallItem(
        response_id="resp-1",
        item_id="item-1",
        call_id="side-effect-call",
        tool_name="apply_patch",
        arguments={"patch": "first"},
    )
    duplicate = LocalToolCallItem(
        response_id="resp-2",
        item_id="item-2",
        call_id="side-effect-call",
        tool_name="apply_patch",
        arguments={"patch": "second"},
    )
    ledger.accept_response(_response("resp-1", first))

    with pytest.raises(ValueError, match="duplicate local tool call id"):
        ledger.accept_response(_response("resp-2", duplicate))

    assert ledger.take_local_calls() == [first]


def test_anonymous_responses_scope_synthetic_item_ids_per_response() -> None:
    ledger = TurnLedger()
    ledger.accept_response(NormalizedModelResponse(
        items=[AssistantMessageItem(item_id="message-0", content="继续", end_turn=False)],
        status="completed",
    ))
    ledger.accept_response(NormalizedModelResponse(
        items=[AssistantMessageItem(item_id="message-0", content="完成", end_turn=True)],
        status="completed",
    ))

    assert ledger.decision().status is TurnStatus.COMPLETED
    assert ledger.decision().assistant_text == "完成"


def test_ledger_snapshot_roundtrip_preserves_committed_and_running_states() -> None:
    ledger = TurnLedger()
    first = LocalToolCallItem(call_id="done", tool_name="read", arguments={"path": "a"})
    second = LocalToolCallItem(call_id="running", tool_name="write", arguments={"path": "b"})
    ledger.accept_response(_response("resp-ledger", first, second))
    ledger.take_local_calls()
    ledger.mark_local_running("done")
    ledger.commit_local_result("done", tool_name="read")
    ledger.mark_local_running("running")

    restored = TurnLedger.from_snapshot(ledger.to_snapshot())

    assert restored.local_status("done") is LocalToolStatus.RESULT_COMMITTED
    assert restored.local_status("running") is LocalToolStatus.RUNNING
    assert restored.take_local_calls() == []


def test_ledger_snapshot_rejects_contradictory_call_identity() -> None:
    ledger = TurnLedger()
    ledger.accept_response(_response(
        "resp-ledger",
        LocalToolCallItem(call_id="call-1", tool_name="write", arguments={"path": "a"}),
    ))
    snapshot = ledger.to_snapshot()
    snapshot["local_calls"][0]["tool_name"] = "delete"

    with pytest.raises(ValueError, match="ledger snapshot"):
        TurnLedger.from_snapshot(snapshot)


@pytest.mark.parametrize(
    "updates",
    [
        {"awaiting_approval": "false"},
        {"background_wait": 1},
        {"stopped": True, "failed": True},
        {"awaiting_approval": True, "background_wait": True},
    ],
)
def test_ledger_snapshot_rejects_non_boolean_and_mutually_exclusive_flags(updates) -> None:
    snapshot = TurnLedger().to_snapshot()
    snapshot.update(updates)
    with pytest.raises(ValueError, match="ledger snapshot"):
        TurnLedger.from_snapshot(snapshot)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: snapshot["local_calls"][0].pop("status"),
        lambda snapshot: snapshot["local_calls"][0].update(observed_by_model=True),
        lambda snapshot: snapshot.update(taken_local_call_ids=[1]),
        lambda snapshot: snapshot["responses"][0].update(items={}),
        lambda snapshot: snapshot.update(hosted_tool_count="0"),
    ],
)
def test_ledger_snapshot_rejects_normalized_call_state(mutate) -> None:
    ledger = TurnLedger()
    ledger.accept_response(_response(
        "resp-ledger",
        LocalToolCallItem(call_id="call-1", tool_name="write", arguments={"path": "a"}),
    ))
    snapshot = ledger.to_snapshot()
    mutate(snapshot)

    with pytest.raises(ValueError, match="ledger snapshot"):
        TurnLedger.from_snapshot(snapshot)
