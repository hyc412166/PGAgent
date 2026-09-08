"""验证上下文默认容量、令牌估算和不完整或重复工具消息组的修复规则。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

from src.context.window import ContextManager, estimate_tokens, message_tokens


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_default_context_limit_is_200k 精确标识本用例的具体条件。
def test_default_context_limit_is_200k() -> None:
    assert ContextManager().max_tokens == 200_000


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_token_estimate_is_conservative_for_unicode 精确标识本用例的具体条件。
def test_token_estimate_is_conservative_for_unicode() -> None:
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("你好") >= 4
    assert message_tokens({"role": "user", "content": "hello"}) > 4


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_repair_keeps_valid_parallel_tool_group_verbatim 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_repair_removes_incomplete_tool_group_atomically 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_repair_removes_duplicate_result_ids 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_repair_removes_orphan_tool_result_without_touching_other_messages 精确标识本用例的具体条件。
def test_repair_removes_orphan_tool_result_without_touching_other_messages() -> None:
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "tool", "tool_call_id": "orphan", "content": "bad"},
        {"role": "user", "content": "continue"},
    ]

    repaired, report = ContextManager().repair_provider_messages(messages)

    assert repaired == [messages[0], messages[2]]
    assert report["affected_call_ids"] == ["orphan"]
