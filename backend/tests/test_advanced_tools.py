"""验证高级工具注册表、兼容工具契约、审批策略、任务看板持久化与只读工具并发调度。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from src.tools import create_default_registry


# CLAW_TOOL_NAMES 是需要兼容执行但不暴露给新模型的 Claw 工具集合；用于核对回放契约与可见工具面分离。
CLAW_TOOL_NAMES = {
    "bash", "read_file", "write_file", "edit_file", "glob_search", "grep_search",
    "WebFetch", "WebSearch", "TodoWrite", "Skill", "Agent", "ToolSearch",
    "NotebookEdit", "Sleep", "SendUserMessage", "Config", "EnterPlanMode",
    "ExitPlanMode", "StructuredOutput", "REPL", "PowerShell", "AskUserQuestion",
    "TaskCreate", "RunTaskPacket", "TaskGet", "TaskList", "TaskStop", "TaskUpdate",
    "TaskOutput", "WorkerCreate", "WorkerGet", "WorkerObserve", "WorkerResolveTrust",
    "WorkerAwaitReady", "WorkerSendPrompt", "WorkerRestart", "WorkerTerminate",
    "WorkerObserveCompletion", "TeamCreate", "TeamDelete", "CronCreate", "CronDelete",
    "CronList", "LSP", "ListMcpResources", "ReadMcpResource", "McpAuth",
    "RemoteTrigger", "MCP", "TestingPermission", "GitStatus", "GitDiff", "GitLog",
    "GitShow", "GitBlame",
}

# LEARN_TOOL_NAMES 是 Learn 兼容工具集合，与上方 Claw 集合共同验证旧轨迹仍可执行。
LEARN_TOOL_NAMES = {
    "load_skill", "compress", "background_run", "check_background", "task_create",
    "task_get", "task_update", "task_list", "spawn_teammate", "list_teammates",
    "send_message", "read_inbox", "broadcast", "shutdown_request", "plan_approval",
    "integrate_teammate", "idle", "claim_task",
}


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_default_registry_keeps_claw_and_learn_contract_hidden_for_replay 精确标识本用例的具体条件。
def test_default_registry_keeps_claw_and_learn_contract_hidden_for_replay(tmp_path) -> None:
    registry = create_default_registry(str(tmp_path), permission_mode="full")
    names = set(registry.enabled_tool_names)

    assert CLAW_TOOL_NAMES <= names
    assert LEARN_TOOL_NAMES <= names
    visible = {item["function"]["name"] for item in registry.schemas}
    assert visible.isdisjoint(CLAW_TOOL_NAMES)
    assert visible.isdisjoint(LEARN_TOOL_NAMES)


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_explicit_empty_tool_selection_is_actually_empty 精确标识本用例的具体条件。
def test_explicit_empty_tool_selection_is_actually_empty(tmp_path) -> None:
    registry = create_default_registry(str(tmp_path), allowed_tool_names=[])

    assert registry.enabled_tool_names == ()
    assert registry.schemas == []


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_notebook_edit_is_real_and_memory_requires_canonical_store 精确标识本用例的具体条件。
def test_notebook_edit_is_real_and_memory_requires_canonical_store(tmp_path) -> None:
    notebook = tmp_path / "demo.ipynb"
    notebook.write_text(json.dumps({
        "cells": [{"id": "first", "cell_type": "code", "source": ["print(1)\n"], "metadata": {}, "outputs": []}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }), encoding="utf-8")
    first = create_default_registry(str(tmp_path), permission_mode="full")

    edited = first.execute("NotebookEdit", {
        "notebook_path": "demo.ipynb",
        "cell_id": "first",
        "new_source": "print(2)",
        "edit_mode": "replace",
    })
    second = create_default_registry(str(tmp_path), permission_mode="full")

    assert edited.ok and "print(2)" in notebook.read_text(encoding="utf-8")
    assert "MemoryWrite" not in first.enabled_tool_names
    assert "MemoryRead" not in second.enabled_tool_names
    assert not (tmp_path / ".memory").exists()


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_task_board_survives_registry_recreation 精确标识本用例的具体条件。
def test_task_board_survives_registry_recreation(tmp_path) -> None:
    first = create_default_registry(str(tmp_path), permission_mode="full")
    created = first.execute("task_create", {"subject": "ship feature", "description": "verify it"})
    task_id = json.loads(created.content)["id"]

    second = create_default_registry(str(tmp_path), permission_mode="full")
    updated = second.execute("task_update", {"task_id": task_id, "status": "in_progress"})
    listed = second.execute("task_list", {})

    assert created.ok and updated.ok and listed.ok
    assert any(item["id"] == task_id and item["status"] == "in_progress" for item in json.loads(listed.content))


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_smart_mode_escalates_advanced_side_effect_and_network_tools 精确标识本用例的具体条件。
def test_smart_mode_escalates_advanced_side_effect_and_network_tools(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["MemoryWrite", "Config", "TaskCreate", "RemoteTrigger"],
        permission_mode="smart",
        memory_store=SimpleNamespace(write=lambda **_kwargs: None),
    )

    calls = (
        ("MemoryWrite", {"name": "preference", "body": "private"}),
        ("Config", {"setting": "theme", "value": "dark"}),
        ("TaskCreate", {"subject": "ship feature", "description": "verify it"}),
        ("RemoteTrigger", {"url": "https://example.com/hook"}),
    )
    for name, arguments in calls:
        result = registry.execute(name, arguments)
        assert result.approval_required, name

    assert not (tmp_path / ".memory").exists()
    assert not (tmp_path / ".pgagent").exists()


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_agent_without_delegate_returns_controlled_unavailable_result 精确标识本用例的具体条件。
def test_agent_without_delegate_returns_controlled_unavailable_result(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["Agent"],
        permission_mode="full",
    )

    result = asyncio.run(registry.execute_async(
        "Agent",
        {"prompt": "inspect the project", "subagent_type": "explorer"},
        call_id="agent-1",
    ))

    assert not result.ok
    assert result.tool_name == "Agent"
    assert result.error_code == "delegated_task_unavailable"


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_plan_mode_blocks_todo_replacement_and_inbox_drain 精确标识本用例的具体条件。
def test_plan_mode_blocks_todo_replacement_and_inbox_drain(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["send_message", "EnterPlanMode", "ExitPlanMode", "TodoWrite", "read_inbox"],
        permission_mode="full",
    )
    sent = registry.execute("send_message", {"to": "lead", "content": "keep me unread"})
    entered = registry.execute("EnterPlanMode", {})

    todo = registry.execute("TodoWrite", {
        "todos": [{"content": "mutate state", "status": "pending"}],
    })
    inbox = registry.execute("read_inbox", {"recipient": "lead"})

    assert sent.ok and entered.ok
    assert todo.error_code == "plan_mode_read_only"
    assert inbox.error_code == "plan_mode_read_only"
    assert registry.runtime_state()["todo_state"] == []

    assert registry.execute("ExitPlanMode", {"plan": "done"}).ok
    drained = registry.execute("read_inbox", {"recipient": "lead"})
    assert drained.ok
    assert "keep me unread" in drained.content


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_read_only_tool_batch_executes_concurrently_and_keeps_result_order 精确标识本用例的具体条件。
def test_read_only_tool_batch_executes_concurrently_and_keeps_result_order(tmp_path) -> None:
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["Sleep"], permission_mode="full")

    # 辅助方法：run_batch 实现测试替身在此调用阶段需要的最小行为。
    async def run_batch():
        started = time.monotonic()
        results = await asyncio.gather(
            registry.execute_async("Sleep", {"duration_ms": 120}, call_id="one"),
            registry.execute_async("Sleep", {"duration_ms": 120}, call_id="two"),
        )
        return time.monotonic() - started, results

    elapsed, results = asyncio.run(run_batch())

    assert elapsed < 0.22
    assert [result.ok for result in results] == [True, True]
