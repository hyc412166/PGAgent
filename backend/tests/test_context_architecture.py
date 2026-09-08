"""验证上下文架构的追加式历史、缓存命名空间、工具结果外置、压缩尾部原子性和新请求优先级。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio

from src.context.assembly import (
    ContextAssembler,
    ConversationCompactor,
    InMemoryArtifactStore,
    ToolOutputBudgeter,
)


# NINE_SECTION_SUMMARY 是压缩器合法输出的固定九段样本，用于验证结构保留和续接优先级。
NINE_SECTION_SUMMARY = """## 1. Primary Request and Intent
finish the cache refactor
## 2. User Corrections and Constraints
None recorded.
## 3. Completed Work
Implemented the first half; step 1 is completed.
## 4. Current Work
Step 2 test is in_progress.
## 5. Pending Tasks
Complete tests.
## 6. Files and Code Sections
Cache implementation.
## 7. Technical Decisions and Problem Solving
Keep an atomic recent tail.
## 8. Errors and Fixes
None recorded.
## 9. Optional Next Step
Run tests."""


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_uncompacted_history_is_append_only_and_keeps_one_cache_namespace 精确标识本用例的具体条件。
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


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_legacy_memory_snapshot_remains_replayable_without_changing_cache_namespace 精确标识本用例的具体条件。
def test_legacy_memory_snapshot_remains_replayable_without_changing_cache_namespace() -> None:
    assembler = ContextAssembler(max_tokens=10_000, output_reserve_tokens=0, safety_buffer_tokens=0)
    stable = assembler.stable_prefix(system_rules="safe", workspace_rules="repo")
    old = assembler.assemble(stable_prefix=stable, transcript=[{
        "role": "user",
        "content": "<memory-context>pytest</memory-context>\n<current-request>run tests</current-request>",
    }])
    new = assembler.assemble(stable_prefix=stable, transcript=[{
        "role": "user",
        "content": "<memory-context>pytest -q</memory-context>\n<current-request>run tests</current-request>",
    }])

    assert old.cache_key == new.cache_key
    assert old.stable_prefix == new.stable_prefix
    assert old.transcript != new.transcript


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_assembler_never_trims_seen_history_when_over_budget 精确标识本用例的具体条件。
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


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_selected_tool_output_is_persisted_with_a_bounded_preview 精确标识本用例的具体条件。
def test_selected_tool_output_is_persisted_with_a_bounded_preview() -> None:
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_compaction_preserves_exact_request_todos_and_atomic_tool_tail 精确标识本用例的具体条件。
def test_compaction_preserves_exact_request_todos_and_atomic_tool_tail() -> None:
    calls: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": NINE_SECTION_SUMMARY}}]}

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
    assert "Authoritative current task state" in continuation["content"]
    assert '"id":"2","content":"test","status":"in_progress"' in continuation["content"]
    assert '"id":"1","content":"implement","status":"completed"' in continuation["content"]
    assert "artifact:" in continuation["content"]
    assert result.messages[-3:] == messages[-3:]
    assert calls[0]["tools"] == []
    assert calls[0]["mode"] == "compaction"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_new_user_request_supersedes_request_inside_old_continuation 精确标识本用例的具体条件。
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
