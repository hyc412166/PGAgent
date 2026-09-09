"""验证上下文服务的产物外置、预算控制、消息组修复、上下文组装、记忆注入和九段式压缩。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio

from src.context.assembly import (
    ContextAssembler,
    ConversationCompactor,
    FilesystemArtifactStore,
    InMemoryArtifactStore,
    ToolOutputBudgeter,
    atomic_message_groups,
    compact_tool_results_for_model,
    retain_recent_atomic_tail,
)
from src.context.window import ContextManager
from src.agent.engine import AgentRuntime, RuntimeConfig
from src.tools.registry import create_default_registry


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_filesystem_artifact_store_survives_a_new_store_instance 精确标识本用例的具体条件。
def test_filesystem_artifact_store_survives_a_new_store_instance(tmp_path) -> None:
    first = FilesystemArtifactStore(tmp_path)
    ref = first.put("durable tool output")

    assert ref.storage_key
    assert FilesystemArtifactStore(tmp_path).get(ref.artifact_id) == b"durable tool output"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_large_tool_output_is_externalized_before_first_model_exposure 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_small_tool_output_remains_verbatim_without_artifact 精确标识本用例的具体条件。
def test_small_tool_output_remains_verbatim_without_artifact() -> None:
    budgeter = ToolOutputBudgeter(InMemoryArtifactStore(), max_chars=256, preview_chars=64)

    prepared = budgeter.prepare(tool_call_id="call-1", tool_name="read_file", output="hello")

    assert prepared.content == "hello"
    assert prepared.artifact_ref is None


# 测试场景：150000 字符以内的单条结果在累计压力未达到阈值时保持原文，避免过早进入 artifact。
def test_single_tool_result_at_or_below_150k_remains_inline_below_aggregate_trigger() -> None:
    messages = [
        {"role": "user", "content": "work"},
        {"role": "tool", "tool_call_id": "small", "name": "read", "content": "x" * 150_000},
    ]
    store = InMemoryArtifactStore()

    result = compact_tool_results_for_model(
        messages,
        budgeter=ToolOutputBudgeter(store),
    )

    assert result.messages is messages
    assert result.changed is False
    assert result.before_chars == 150_000
    assert result.after_chars == 150_000
    assert result.artifact_refs == []


# 测试场景：单条结果超过 150000 字符时，即使累计结果未超过 300000，也会在下一轮模型调用前外部化。
def test_single_tool_result_over_150k_is_externalized_below_aggregate_trigger() -> None:
    content = "x" * 150_001
    messages = [{"role": "tool", "tool_call_id": "large", "name": "read", "content": content}]
    store = InMemoryArtifactStore()

    result = compact_tool_results_for_model(messages, budgeter=ToolOutputBudgeter(store))

    assert result.changed is True
    assert result.before_chars == 150_001
    assert result.messages[0]["content"].startswith("<persisted-tool-output>")
    assert store.get(result.artifact_refs[0].artifact_id) == content.encode()


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_tool_results_are_externalized_largest_first_until_target 精确标识本用例的具体条件。
def test_tool_results_are_externalized_largest_first_until_target() -> None:
    contents = ["a" * 150_000, "b" * 100_000, "c" * 60_001]
    messages = [
        {"role": "tool", "tool_call_id": f"call-{index}", "name": "read", "content": content}
        for index, content in enumerate(contents)
    ]
    store = InMemoryArtifactStore()

    result = compact_tool_results_for_model(
        messages,
        budgeter=ToolOutputBudgeter(store),
        keep_recent_tool_results=0,
    )

    assert result.changed is True
    # 累计压力下按最大结果优先处理，达到 150000 目标后停止。
    assert result.compacted_count == 2
    assert result.before_chars == 310_001
    assert result.after_chars <= 150_000
    assert result.messages[0]["content"].startswith("<persisted-tool-output>")
    assert result.messages[1]["content"].startswith("<persisted-tool-output>")
    assert result.messages[2]["content"] == contents[2]
    assert messages[0]["content"] == contents[0]
    assert [store.get(ref.artifact_id) for ref in result.artifact_refs] == [
        contents[0].encode(),
        contents[1].encode(),
    ]


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_two_most_recent_tool_results_are_still_bounded_individually 精确标识本用例的具体条件。
def test_two_most_recent_tool_results_are_still_bounded_individually() -> None:
    contents = ["a" * 160_000, "b" * 80_000, "c" * 70_000]
    messages = [
        {"role": "tool", "tool_call_id": f"call-{index}", "name": "read", "content": content}
        for index, content in enumerate(contents)
    ]

    result = compact_tool_results_for_model(
        messages,
        budgeter=ToolOutputBudgeter(InMemoryArtifactStore()),
    )

    assert result.messages[0]["content"].startswith("<persisted-tool-output>")
    # 默认保留最近两条结果，只有超过 150000 的首条会提前外部化。
    assert result.messages[1]["content"] == contents[1]
    assert result.messages[2]["content"] == contents[2]
    assert result.compacted_count == 1
    assert result.target_reached is False


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_small_aggregate_results_pass_through_when_preview_cannot_reduce 精确标识本用例的具体条件。
def test_small_aggregate_results_pass_through_when_preview_cannot_reduce() -> None:
    messages = [
        {"role": "tool", "tool_call_id": f"call-{index}", "name": "read", "content": "x" * 1_001}
        for index in range(301)
    ]

    result = compact_tool_results_for_model(
        messages,
        budgeter=ToolOutputBudgeter(InMemoryArtifactStore()),
        keep_recent_tool_results=0,
    )

    assert result.before_chars > 300_000
    assert result.after_chars == result.before_chars
    assert result.compacted_count == 0
    assert result.target_reached is False
    assert result.artifact_refs == []
    assert result.messages == messages


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_existing_artifact_previews_are_not_shortened_again 精确标识本用例的具体条件。
def test_existing_artifact_previews_are_not_shortened_again() -> None:
    budgeter = ToolOutputBudgeter(InMemoryArtifactStore())
    previews = [
        budgeter.externalize(
            tool_call_id=f"call-{index}",
            tool_name="read",
            output=f"result-{index}-" + ("x" * 4_000),
        ).content
        for index in range(151)
    ]
    messages = [
        {"role": "tool", "tool_call_id": f"call-{index}", "name": "read", "content": content}
        for index, content in enumerate(previews)
    ]

    result = compact_tool_results_for_model(
        messages,
        budgeter=budgeter,
        keep_recent_tool_results=0,
    )

    assert result.before_chars > 300_000
    assert result.after_chars == result.before_chars
    assert result.target_reached is False
    assert result.artifact_refs == []
    assert result.messages == messages


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_medium_results_keep_full_previews_when_target_is_unreachable 精确标识本用例的具体条件。
def test_medium_results_keep_full_previews_when_target_is_unreachable() -> None:
    messages = [
        {"role": "tool", "tool_call_id": f"call-{index}", "name": "read", "content": "x" * 4_000}
        for index in range(76)
    ]

    result = compact_tool_results_for_model(
        messages,
        budgeter=ToolOutputBudgeter(InMemoryArtifactStore()),
        keep_recent_tool_results=0,
    )

    assert result.before_chars == 304_000
    assert result.after_chars > 150_000
    assert result.target_reached is False
    assert result.compacted_count == 76
    previews = [str(item["content"]) for item in result.messages]
    assert all("preview:\n" + ("x" * 2_000) in content for content in previews)
    assert all("preview:\n\n</persisted-tool-output>" not in content for content in previews)


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_assembler_has_stable_cache_namespace_and_append_only_transcript 精确标识本用例的具体条件。
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


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_assembler_defaults_to_200k_with_180k_compaction_threshold 精确标识本用例的具体条件。
def test_assembler_defaults_to_200k_with_180k_compaction_threshold() -> None:
    assembler = ContextAssembler()

    assert assembler.max_tokens == 200_000
    assert assembler.compaction_threshold == 180_000


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_assembler_reports_compaction_without_mutating_seen_messages 精确标识本用例的具体条件。
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


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_runtime_config_propagates_explicit_compaction_threshold 精确标识本用例的具体条件。
def test_runtime_config_propagates_explicit_compaction_threshold(tmp_path) -> None:
    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_memory_index_is_late_system_context_without_changing_cache_namespace 精确标识本用例的具体条件。
def test_memory_index_is_late_system_context_without_changing_cache_namespace(tmp_path) -> None:
    calls: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "done"}}]}

    # 辅助方法：execute 实现测试替身在此调用阶段需要的最小行为。
    async def execute(index: str):
        runtime = AgentRuntime(
            model_call=model_call,
            tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        )
        return await runtime.run(
            system_prompt="system",
            workspace_rules="workspace",
            memory_index=index,
            recent_messages=[{"role": "user", "content": "question"}],
        )

    first = asyncio.run(execute("- Memory A"))
    second = asyncio.run(execute("- Memory B"))

    first_key = next(event["prompt_cache_key"] for event in first.events if event["type"] == "context_prepared")
    second_key = next(event["prompt_cache_key"] for event in second.events if event["type"] == "context_prepared")
    assert first_key == second_key
    assert calls[0]["messages"][-2]["role"] == "system"
    assert "Memory A" in calls[0]["messages"][-2]["content"]
    assert calls[0]["messages"][-1] == {"role": "user", "content": "question"}


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_runtime_strips_memory_citation_before_accepting_reply 精确标识本用例的具体条件。
def test_runtime_strips_memory_citation_before_accepting_reply(tmp_path) -> None:
    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs):
        return {"choices": [{"message": {"content": (
            'Visible answer.\n<pgagent-memory-citation>{"memory_ids":["m1"],'
            '"rollout_ids":[],"note":"used"}</pgagent-memory-citation>'
        )}}]}

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
    )
    outcome = asyncio.run(runtime.run(
        system_prompt="system",
        recent_messages=[{"role": "user", "content": "question"}],
    ))

    assert outcome.output == "Visible answer."
    assert outcome.messages[-1]["content"] == "Visible answer."
    assert outcome.memory_citation["memory_ids"] == ["m1"]
    assert outcome.memory_citation["skill_ids"] == []


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_runtime_strips_premature_memory_citation_from_tool_call_text 精确标识本用例的具体条件。
def test_runtime_strips_premature_memory_citation_from_tool_call_text(tmp_path) -> None:
    calls = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"choices": [{"message": {
                "content": 'working<pgagent-memory-citation>{"memory_ids":["m1"]}</pgagent-memory-citation>',
                "tool_calls": [{
                    "id": "t1", "type": "function",
                    "function": {"name": "get_current_time", "arguments": "{}"},
                }],
            }}]}
        return {"choices": [{"message": {"content": "done"}}]}

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=["get_current_time"]),
    )
    outcome = asyncio.run(runtime.run(
        system_prompt="system",
        recent_messages=[{"role": "user", "content": "question"}],
    ))

    assert outcome.status == "completed"
    assert all("pgagent-memory-citation" not in str(message.get("content") or "") for message in outcome.messages)


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_atomic_groups_keep_complete_parallel_tool_batch 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_corrupt_tool_groups_are_not_retained_as_provider_history 精确标识本用例的具体条件。
def test_corrupt_tool_groups_are_not_retained_as_provider_history() -> None:
    corrupt = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "one", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "different", "content": "bad"},
    ]

    assert atomic_message_groups(corrupt) == []


# NINE_SECTION_SUMMARY 为上下文压缩测试复用的合法九段摘要，作为模型返回和结构断言的基准数据。
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
backend/src/context/assembly.py
## 7. Technical Decisions and Problem Solving
Retain an atomic recent tail.
## 8. Errors and Fixes
None recorded.
## 9. Optional Next Step
Run the focused tests."""


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_compactor_uses_original_system_prefix_and_merges_task_state_into_nine_sections 精确标识本用例的具体条件。
def test_compactor_uses_original_system_prefix_and_merges_task_state_into_nine_sections() -> None:
    calls: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
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
    assert "Write as concisely as possible while preserving every fact" in prompt[-1]["content"]
    assert "Never omit an unresolved ambiguity" in prompt[-1]["content"]
    assert "token" not in prompt[-1]["content"].lower()
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


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_compactor_retain_tokens_limits_tail_without_splitting_tool_group 精确标识本用例的具体条件。
def test_compactor_retain_tokens_limits_tail_without_splitting_tool_group() -> None:
    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_compactor_fallback_merges_exact_continuation_state_into_nine_sections 精确标识本用例的具体条件。
def test_compactor_fallback_merges_exact_continuation_state_into_nine_sections() -> None:
    # 辅助方法：unavailable 实现测试替身在此调用阶段需要的最小行为。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_compactor_rejects_extra_headings_and_escapes_wrapper_delimiters 精确标识本用例的具体条件。
def test_compactor_rejects_extra_headings_and_escapes_wrapper_delimiters() -> None:
    # 辅助方法：malformed 实现测试替身在此调用阶段需要的最小行为。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_compactor_rejects_setext_heading_as_a_tenth_section 精确标识本用例的具体条件。
def test_compactor_rejects_setext_heading_as_a_tenth_section() -> None:
    # 辅助方法：malformed 实现测试替身在此调用阶段需要的最小行为。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_empty_current_board_replaces_stale_pending_work_from_model_summary 精确标识本用例的具体条件。
def test_empty_current_board_replaces_stale_pending_work_from_model_summary() -> None:
    stale = NINE_SECTION_SUMMARY.replace("Finish tests.", "Finish the previous task A.")

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
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
