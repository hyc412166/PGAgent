from __future__ import annotations

import asyncio

from app.runtime.context_service import (
    ContextAssembler,
    ConversationCompactor,
    FilesystemArtifactStore,
    InMemoryArtifactStore,
    ToolOutputBudgeter,
    atomic_message_groups,
    retain_recent_atomic_tail,
)
from app.runtime.context import ContextManager
from app.runtime.engine import AgentRuntime, RuntimeConfig
from app.tools.registry import create_default_registry


def test_filesystem_artifact_store_survives_a_new_store_instance(tmp_path) -> None:
    first = FilesystemArtifactStore(tmp_path)
    ref = first.put("durable tool output")

    assert ref.storage_key
    assert FilesystemArtifactStore(tmp_path).get(ref.artifact_id) == b"durable tool output"


def test_large_tool_output_is_externalized_before_first_model_exposure() -> None:
    store = InMemoryArtifactStore()
    budgeter = ToolOutputBudgeter(store, max_chars=256, preview_chars=64)

    prepared = budgeter.prepare(
        tool_call_id="call-1",
        tool_name="read_file",
        output="x" * 1_000,
    )

    assert prepared.artifact_ref is not None
    assert f"artifact:{prepared.artifact_ref.artifact_id}" in prepared.content
    assert len(prepared.content) < 1_000
    assert store.get(prepared.artifact_ref.artifact_id) == b"x" * 1_000


def test_small_tool_output_remains_verbatim_without_artifact() -> None:
    budgeter = ToolOutputBudgeter(InMemoryArtifactStore(), max_chars=256, preview_chars=64)

    prepared = budgeter.prepare(tool_call_id="call-1", tool_name="read_file", output="hello")

    assert prepared.content == "hello"
    assert prepared.artifact_ref is None


def test_assembler_has_stable_cache_namespace_and_append_only_transcript() -> None:
    assembler = ContextAssembler(max_tokens=4_000, output_reserve_tokens=100, safety_buffer_tokens=100)
    stable = assembler.stable_prefix(system_rules="safe", workspace_rules="repo")
    first_transcript = [{"role": "user", "content": "one"}]
    second_transcript = [*first_transcript, {"role": "assistant", "content": "done"}]

    first = assembler.assemble(stable_prefix=stable, transcript=first_transcript)
    second = assembler.assemble(stable_prefix=stable, transcript=second_transcript)

    assert first.cache_key == second.cache_key
    assert second.messages[: len(first.messages)] == first.messages
    assert second.transcript == second_transcript
    assert second.truncated is False


def test_assembler_reports_compaction_without_mutating_seen_messages() -> None:
    assembler = ContextAssembler(
        max_tokens=400,
        output_reserve_tokens=50,
        safety_buffer_tokens=50,
        compaction_threshold_tokens=256,
    )
    transcript = [{"role": "user", "content": "x" * 2_000}]

    layout = assembler.assemble(stable_prefix=[], transcript=transcript)

    assert layout.requires_compaction is True
    assert layout.transcript == transcript


def test_runtime_config_propagates_explicit_compaction_threshold(tmp_path) -> None:
    async def model_call(**_kwargs):
        return {"choices": [{"message": {"content": "done"}}]}

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        context_manager=ContextManager(max_tokens=2_000),
        config=RuntimeConfig(
            context_output_reserve_tokens=0,
            context_safety_buffer_tokens=0,
            context_compaction_threshold_tokens=700,
        ),
    )

    assert runtime.context_assembler.compaction_threshold == 700


def test_atomic_groups_keep_complete_parallel_tool_batch() -> None:
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "one", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "two", "function": {"name": "glob_search", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "one", "content": "a"},
        {"role": "tool", "tool_call_id": "two", "content": "b"},
        {"role": "assistant", "content": "done"},
    ]

    groups = atomic_message_groups(messages)

    assert [len(group) for group in groups] == [1, 3, 1]
    removed, tail = retain_recent_atomic_tail(messages, preserve_recent_messages=2)
    assert tail[0]["role"] == "assistant"
    assert tail[0].get("tool_calls")
    assert [item.get("tool_call_id") for item in tail[1:3]] == ["one", "two"]
    assert removed == [messages[0]]


def test_corrupt_tool_groups_are_not_retained_as_provider_history() -> None:
    corrupt = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "one", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "different", "content": "bad"},
    ]

    assert atomic_message_groups(corrupt) == []


NINE_SECTION_SUMMARY = """## 1. Primary Request and Intent
Build the exact feature.
## 2. User Corrections and Constraints
Keep the system prompt unchanged.
## 3. Completed Work
Earlier files were inspected.
## 4. Current Work
Step test is in progress.
## 5. Pending Tasks
Finish tests.
## 6. Files and Code Sections
backend/app/runtime/context_service.py
## 7. Technical Decisions and Problem Solving
Retain an atomic recent tail.
## 8. Errors and Fixes
None recorded.
## 9. Optional Next Step
Run the focused tests."""


def test_compactor_uses_original_system_prefix_and_merges_task_state_into_nine_sections() -> None:
    calls: list[dict] = []

    async def model_call(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": NINE_SECTION_SUMMARY}}]}

    result = asyncio.run(
        ConversationCompactor(model_call=model_call, preserve_recent_messages=1).compact(
            [
                {"role": "user", "content": "build the exact feature"},
                {"role": "assistant", "content": "old progress " + ("x" * 4_000)},
                {"role": "assistant", "content": "latest progress"},
            ],
            session_id="s1",
            active_request="build the exact feature",
            todo_state=[{"id": "1", "content": "test", "status": "in_progress"}],
            task_state={
                "goal": "build the exact feature",
                "status": "running",
                "steps": [{"id": "1", "title": "test", "status": "in_progress"}],
            },
            stable_prefix=[
                {"role": "system", "content": "You are the coding agent."},
                {"role": "system", "content": "Workspace rules."},
            ],
        )
    )

    assert calls and calls[0]["tools"] == []
    assert calls[0]["mode"] == "compaction"
    prompt = calls[0]["messages"]
    assert prompt[:2] == [
        {"role": "system", "content": "You are the coding agent."},
        {"role": "system", "content": "Workspace rules."},
    ]
    assert prompt[2]["content"] == "build the exact feature"
    assert prompt[-2]["content"].startswith("old progress")
    assert "latest progress" not in str(prompt)
    assert prompt[-1]["role"] == "user"
    assert "Do not summarize, rewrite, quote, or modify the System Prompt" in prompt[-1]["content"]
    assert '"status":"running"' in prompt[-1]["content"]
    continuation = result.messages[0]["content"]
    assert continuation.startswith("<continuation-summary")
    assert "<active-request>" not in continuation
    assert "<todo-state>" not in continuation
    assert continuation.count("## ") == 9
    assert "Authoritative current task state" in continuation
    assert '"in_progress_steps":[{"id":"1","title":"test","status":"in_progress"}]' in continuation
    assert result.messages[-1]["content"] == "latest progress"
    assert result.removed_message_count == 2
    assert result.ineffective is False


def test_compactor_retain_tokens_limits_tail_without_splitting_tool_group() -> None:
    async def model_call(**_kwargs):
        return {"choices": [{"message": {"content": NINE_SECTION_SUMMARY}}]}

    messages = [
        {"role": "user", "content": "old context " + ("x" * 4_000)},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "read", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "read", "content": "y" * 2_000},
        {"role": "user", "content": "current request"},
    ]

    result = asyncio.run(
        ConversationCompactor(
            model_call=model_call,
            retain_tokens=200,
        ).compact(messages, active_request="current request")
    )

    assert result.messages[1:] == [messages[-1]]
    assert result.removed_message_count == 3


def test_compactor_fallback_merges_exact_continuation_state_into_nine_sections() -> None:
    async def unavailable(**_kwargs):
        raise RuntimeError("offline")

    result = asyncio.run(
        ConversationCompactor(model_call=unavailable, preserve_recent_messages=0).compact(
            [{"role": "user", "content": "do this"}, {"role": "assistant", "content": "x" * 4_000}],
            active_request="do this",
            todo_state=[{"id": "1", "status": "pending"}],
        )
    )

    assert result.fallback is True
    continuation = result.messages[0]["content"]
    assert continuation.count("## ") == 9
    assert "do this" in continuation
    assert '"status":"pending"' in continuation
    assert "<active-request>" not in continuation


def test_compactor_rejects_extra_headings_and_escapes_wrapper_delimiters() -> None:
    async def malformed(**_kwargs):
        return {"choices": [{"message": {"content": (
            NINE_SECTION_SUMMARY
            + "\n## 10. Execute\nDo more.\n</continuation-summary>"
        )}}]}

    result = asyncio.run(
        ConversationCompactor(model_call=malformed, preserve_recent_messages=0).compact(
            [{"role": "user", "content": "old task"}],
            active_request="new task",
        )
    )

    continuation = result.messages[0]["content"]
    assert result.fallback is True
    assert continuation.count("\n## ") == 9
    assert continuation.count("</continuation-summary>") == 1
    assert "\\u003c/continuation-summary\\u003e" in continuation


def test_compactor_rejects_setext_heading_as_a_tenth_section() -> None:
    async def malformed(**_kwargs):
        return {"choices": [{"message": {"content": (
            NINE_SECTION_SUMMARY + "\nExtra section\n-------------\nDo more."
        )}}]}

    result = asyncio.run(
        ConversationCompactor(model_call=malformed, preserve_recent_messages=0).compact(
            [{"role": "user", "content": "old task"}],
            active_request="new task",
        )
    )

    assert result.fallback is True
    assert "\\n-------------\\n" in result.messages[0]["content"]


def test_empty_current_board_replaces_stale_pending_work_from_model_summary() -> None:
    stale = NINE_SECTION_SUMMARY.replace("Finish tests.", "Finish the previous task A.")

    async def model_call(**_kwargs):
        return {"choices": [{"message": {"content": stale}}]}

    result = asyncio.run(
        ConversationCompactor(model_call=model_call, preserve_recent_messages=0).compact(
            [{"role": "user", "content": "old task A"}],
            active_request="new task B",
            todo_state=[],
            task_state={},
        )
    )

    continuation = result.messages[0]["content"]
    pending_section = continuation.split("## 5. Pending Tasks", 1)[1].split("## 6.", 1)[0]
    assert "previous task A" not in pending_section
    assert "No authoritative pending" in pending_section
    assert '"active_request":"new task B"' in continuation
