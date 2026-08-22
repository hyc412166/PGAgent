from __future__ import annotations

import asyncio

from app.runtime.context_service import (
    ContextAssembler,
    ConversationCompactor,
    InMemoryArtifactStore,
    ToolOutputBudgeter,
)


def test_uncompacted_history_is_append_only_and_keeps_one_cache_namespace() -> None:
    assembler = ContextAssembler(max_tokens=10_000, output_reserve_tokens=0, safety_buffer_tokens=0)
    stable = assembler.stable_prefix(
        system_rules="safe",
        workspace_rules="repo",
        permission_policy="full access",
        extra_messages=[{"role": "system", "content": "agent instructions"}],
    )
    first_transcript = [{"role": "user", "content": "first task"}]
    second_transcript = [
        *first_transcript,
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "second task"},
    ]

    first = assembler.assemble(stable_prefix=stable, transcript=first_transcript)
    second = assembler.assemble(stable_prefix=stable, transcript=second_transcript)

    assert second.messages[: len(first.messages)] == first.messages
    assert first.cache_key == second.cache_key
    assert first.requires_compaction is False
    assert second.requires_compaction is False


def test_assembler_never_trims_seen_history_when_over_budget() -> None:
    assembler = ContextAssembler(max_tokens=400, output_reserve_tokens=0, safety_buffer_tokens=0)
    stable = assembler.stable_prefix(system_rules="safe")
    transcript = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "x" * 8_000},
    ]

    layout = assembler.assemble(stable_prefix=stable, transcript=transcript)

    assert layout.messages == [*stable, *transcript]
    assert layout.requires_compaction is True
    assert layout.truncated is False


def test_large_tool_output_is_bounded_before_first_model_exposure() -> None:
    store = InMemoryArtifactStore()
    budgeter = ToolOutputBudgeter(store, max_chars=1_000, preview_chars=200)
    output = "important-output\n" * 2_000

    first = budgeter.prepare(tool_call_id="call-1", tool_name="read", output=output)
    second = budgeter.prepare(tool_call_id="call-1", tool_name="read", output=output)

    assert first.content == second.content
    assert len(first.content) < 1_000
    assert "artifact:" in first.content
    assert "important-output" in first.content
    assert first.artifact_ref is not None
    assert store.get(first.artifact_ref.artifact_id) == output.encode()


def test_compaction_preserves_exact_request_todos_and_atomic_tool_tail() -> None:
    calls: list[dict] = []

    async def model_call(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "Implemented the first half; tests remain."}}]}

    messages = [
        {"role": "user", "content": "old discussion"},
        {"role": "assistant", "content": "old answer"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "read-1", "function": {"name": "read", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "read-1", "name": "read", "content": "file data"},
        {"role": "user", "content": "finish the cache refactor"},
    ]
    todos = [
        {"id": "1", "content": "implement", "status": "completed"},
        {"id": "2", "content": "test", "status": "in_progress"},
    ]
    compactor = ConversationCompactor(
        model_call=model_call,
        artifact_store=InMemoryArtifactStore(),
        preserve_recent_messages=3,
    )

    result = asyncio.run(
        compactor.compact(
            messages,
            active_request="finish the cache refactor",
            todo_state=todos,
            session_id="session-1",
        )
    )

    continuation = result.messages[0]
    assert continuation["role"] == "user"
    assert "finish the cache refactor" in continuation["content"]
    assert '"status":"in_progress"' in continuation["content"]
    assert "Implemented the first half" in continuation["content"]
    assert "artifact:" in continuation["content"]
    assert result.messages[-3:] == messages[-3:]
    assert calls[0]["tools"] == []
    assert calls[0]["mode"] == "compaction"


def test_new_user_request_supersedes_request_inside_old_continuation() -> None:
    assembler = ContextAssembler(max_tokens=10_000, output_reserve_tokens=0, safety_buffer_tokens=0)
    stable = assembler.stable_prefix(system_rules="latest ordinary user request wins")
    old_continuation = {
        "role": "user",
        "content": "<compacted-context><active-request>first task</active-request></compacted-context>",
    }
    transcript = [
        old_continuation,
        {"role": "assistant", "content": "first task completed"},
        {"role": "user", "content": "second task"},
    ]

    layout = assembler.assemble(stable_prefix=stable, transcript=transcript)

    assert layout.messages[-1]["content"] == "second task"
    assert layout.messages.count(old_continuation) == 1

