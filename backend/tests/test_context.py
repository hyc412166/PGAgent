from __future__ import annotations

import hashlib

from app.runtime.context import (
    DEFAULT_COMPACT_THRESHOLD_TOKENS,
    DEFAULT_CONTEXT_LIMIT_TOKENS,
    ContextManager,
    estimate_tokens,
    message_tokens,
)


def test_default_context_budget_is_100k_with_90_percent_compaction_threshold() -> None:
    assert ContextManager().max_tokens == 100_000
    assert DEFAULT_CONTEXT_LIMIT_TOKENS == 100_000
    assert DEFAULT_COMPACT_THRESHOLD_TOKENS == 90_000


def test_estimator_counts_cjk_without_undercounting_to_zero() -> None:
    text = "这是中文上下文"
    assert estimate_tokens(text) >= len(text)
    emoji = "😀" * 100
    assert estimate_tokens(emoji) >= len(emoji) * 2


def test_context_contains_layers_and_recent_messages() -> None:
    manager = ContextManager(max_tokens=1_200)
    result = manager.build(
        system_prompt="保护工作区",
        agent_instructions="先验证再修改",
        workspace_rules="禁止越界",
        summary="用户正在实现 Agent",
        memories=[{"content": "偏好中文", "pinned": True}],
        recent_messages=[{"role": "user", "content": "继续工作"}],
    )
    rendered = "\n".join(str(item["content"]) for item in result.messages)
    assert "系统规则" in rendered
    assert "Agent 配置" in rendered
    assert "工作区规则" in rendered
    assert "会话摘要" in rendered
    assert "[固定] 偏好中文" in rendered
    assert result.messages[-1] == {"role": "user", "content": "继续工作"}


def test_context_drops_oldest_turns_and_keeps_newest_suffix() -> None:
    manager = ContextManager(max_tokens=300, recent_ratio=0.8)
    recent = [
        {"role": "user", "content": f"message-{index} " + ("x" * 180)}
        for index in range(8)
    ]
    result = manager.build(system_prompt="safe", recent_messages=recent)
    recent_kept = [item for item in result.messages if item.get("role") == "user"]
    assert result.truncated
    assert result.omitted_messages > 0
    assert recent_kept
    assert recent_kept[-1]["content"].startswith("message-7")
    assert result.estimated_tokens <= 300


def test_very_long_system_prompt_stays_within_hard_budget() -> None:
    manager = ContextManager(max_tokens=256)
    result = manager.build(system_prompt="安全规则" * 10_000)
    assert result.truncated
    assert result.estimated_tokens <= 256


def test_multiple_long_system_layers_never_exceed_hard_budget() -> None:
    manager = ContextManager(max_tokens=256)
    result = manager.build(
        system_prompt="x" * 10_000,
        agent_instructions="y" * 10_000,
        workspace_rules="z" * 10_000,
        summary="s" * 10_000,
        recent_messages=[{"role": "user", "content": "latest"}],
    )
    assert result.truncated
    assert result.estimated_tokens <= 256
    assert sum(message_tokens(message) for message in result.messages) <= 256


def test_compaction_is_bounded() -> None:
    manager = ContextManager()
    summary = manager.compact_messages(
        [{"role": "user", "content": "x" * 5_000}],
        token_budget=100,
    )
    assert estimate_tokens(summary) <= 110  # includes the explicit truncation marker


def test_compaction_represents_every_row_before_cursor_advances() -> None:
    messages = [
        {"role": "user", "content": f"row-{index}-" + (str(index) * 1_000)}
        for index in range(4)
    ]
    summary = ContextManager().compact_messages(messages, token_budget=240)
    for message in messages:
        digest = hashlib.sha256(message["content"].encode("utf-8")).hexdigest()[:12]
        assert digest in summary
    assert estimate_tokens(summary) <= 240


def test_many_short_messages_use_one_digest_covering_the_full_transcript() -> None:
    messages = [
        {"role": "user", "content": f"message-{index}"}
        for index in range(6_000)
    ]
    identities = []
    for message in messages:
        digest = hashlib.sha256(message["content"].encode("utf-8")).hexdigest()[:12]
        identities.append(f"user\0{digest}")
    transcript_digest = hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()

    summary = ContextManager().compact_messages(messages, token_budget=12_000)
    assert summary == f"6000 messages [sha256:{transcript_digest}]"
    assert estimate_tokens(summary) <= 12_000


def test_runtime_trim_preserves_tool_call_and_result_as_a_group() -> None:
    manager = ContextManager(max_tokens=340)
    messages = [
        {"role": "system", "content": "safe"},
        {"role": "user", "content": "old " + "x" * 500},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "y" * 900},
    ]
    trimmed, omitted = manager.trim_runtime_messages(messages)
    roles = [message["role"] for message in trimmed]
    assert roles[-2:] == ["assistant", "tool"]
    assert omitted >= 1
    assert sum(message_tokens(item) for item in trimmed) <= 340


def test_initial_build_keeps_detached_tool_result_with_its_call() -> None:
    manager = ContextManager(max_tokens=256, recent_ratio=0.8)
    result = manager.build(
        system_prompt="safe",
        recent_messages=[
            {"role": "user", "content": "old " + "x" * 600},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
            },
        ],
        tool_results=[
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "y" * 800}
        ],
    )
    roles = [message["role"] for message in result.messages]
    assert roles[-2:] == ["assistant", "tool"]
    assert result.messages[-1]["tool_call_id"] == "c1"
    assert result.estimated_tokens <= 256


def test_initial_build_drops_orphan_tool_result() -> None:
    manager = ContextManager(max_tokens=512)
    result = manager.build(
        system_prompt="safe",
        recent_messages=[{"role": "user", "content": "hello"}],
        tool_results=[{"role": "tool", "tool_call_id": "missing", "content": "orphan"}],
    )
    assert all(message.get("role") != "tool" for message in result.messages)
    assert result.omitted_messages >= 1


def test_runtime_trim_drops_incomplete_tool_call_group() -> None:
    manager = ContextManager(max_tokens=512)
    trimmed, omitted = manager.trim_runtime_messages([
        {"role": "system", "content": "safe"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "a", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "b", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "only one"},
    ])
    assert [message["role"] for message in trimmed] == ["system"]
    assert omitted == 2
