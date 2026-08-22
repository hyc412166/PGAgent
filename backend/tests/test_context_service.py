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


def test_compactor_keeps_exact_request_and_todos_outside_model_summary() -> None:
    calls: list[dict] = []

    async def model_call(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "Earlier files were inspected."}}]}

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
        )
    )

    assert calls and calls[0]["tools"] == []
    assert calls[0]["mode"] == "compaction"
    continuation = result.messages[0]["content"]
    assert "<active-request>build the exact feature</active-request>" in continuation
    assert '"status":"in_progress"' in continuation
    assert "Earlier files were inspected." in continuation
    assert result.messages[-1]["content"] == "latest progress"
    assert result.removed_message_count == 2
    assert result.ineffective is False


def test_compactor_fallback_still_preserves_exact_continuation_state() -> None:
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
    assert "<active-request>do this</active-request>" in result.messages[0]["content"]
    assert '"status":"pending"' in result.messages[0]["content"]
