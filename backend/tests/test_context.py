from __future__ import annotations

from src.context.window import ContextManager, estimate_tokens, message_tokens


def test_default_context_limit_is_100k() -> None:
    assert ContextManager().max_tokens == 100_000


def test_token_estimate_is_conservative_for_unicode() -> None:
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("你好") >= 4
    assert message_tokens({"role": "user", "content": "hello"}) > 4


def test_repair_keeps_valid_parallel_tool_group_verbatim() -> None:
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "b", "function": {"name": "glob_search", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "one"},
        {"role": "tool", "tool_call_id": "b", "content": "two"},
    ]

    repaired, report = ContextManager().repair_provider_messages(messages)

    assert repaired == messages
    assert report == {"removed_messages": 0, "affected_call_ids": []}


def test_repair_removes_incomplete_tool_group_atomically() -> None:
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "a", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "b", "function": {"name": "glob_search", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "one"},
        {"role": "assistant", "content": "later"},
    ]

    repaired, report = ContextManager().repair_provider_messages(messages)

    assert repaired == [messages[0], messages[-1]]
    assert report["removed_messages"] == 2
    assert report["affected_call_ids"] == ["a", "b"]


def test_repair_removes_duplicate_result_ids() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "a", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "a", "content": "one"},
        {"role": "tool", "tool_call_id": "a", "content": "duplicate"},
    ]

    repaired, report = ContextManager().repair_provider_messages(messages)

    assert repaired == []
    assert report["removed_messages"] == 3
    assert report["affected_call_ids"] == ["a"]


def test_repair_removes_orphan_tool_result_without_touching_other_messages() -> None:
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "tool", "tool_call_id": "orphan", "content": "bad"},
        {"role": "user", "content": "continue"},
    ]

    repaired, report = ContextManager().repair_provider_messages(messages)

    assert repaired == [messages[0], messages[2]]
    assert report["affected_call_ids"] == ["orphan"]
