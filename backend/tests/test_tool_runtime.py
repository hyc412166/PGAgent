from __future__ import annotations

import pytest

from app.runtime.engine import AgentRuntime, ModelToolCall, ModelTurn
from app.tools import create_default_registry


def test_registry_exposes_only_selected_tools_and_enforces_permission_modes(tmp_path) -> None:
    ask_registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read", "write", "webfetch"],
        permission_mode="ask",
    )
    assert [item["function"]["name"] for item in ask_registry.schemas] == ["read", "write", "webfetch"]
    assert ask_registry.execute("grep", {"pattern": "x"}).error_code == "tool_not_enabled"
    assert ask_registry.execute("write", {"path": "draft.txt", "content": "secret"}).approval_required
    assert ask_registry.execute("webfetch", {"url": "https://example.com"}).approval_required

    smart_registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["webfetch"],
        permission_mode="smart",
    )
    # Smart mode lets an otherwise safe network tool run, but the tool itself
    # still rejects SSRF targets before sending a request.
    blocked = smart_registry.execute("webfetch", {"url": "http://127.0.0.1:8765"})
    assert not blocked.ok
    assert blocked.error_code == "unsafe_url"

    full_registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["write", "bash"],
        permission_mode="full",
    )
    written = full_registry.execute("write", {"path": "draft.txt", "content": "written"})
    assert written.ok
    # Full removes the confirmation prompt only.  It never enables a shell or
    # bypasses the local executable allowlist.
    blocked_command = full_registry.execute("bash", {"command": ["not-an-allowed-command"]})
    assert not blocked_command.ok
    assert blocked_command.error_code == "command_not_allowed"


def test_edit_glob_and_grep_are_real_sandboxed_tools(tmp_path) -> None:
    (tmp_path / "notes").mkdir()
    target = tmp_path / "notes" / "sample.txt"
    target.write_text("one needle\ntwo needle\n", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["edit", "glob", "grep"],
        permission_mode="full",
    )

    ambiguous = registry.execute(
        "edit",
        {"path": "notes/sample.txt", "old_string": "needle", "new_string": "pin"},
    )
    assert ambiguous.error_code == "edit_not_unique"
    edited = registry.execute(
        "edit",
        {"path": "notes/sample.txt", "old_string": "needle", "new_string": "pin", "replace_all": True},
    )
    assert edited.ok and edited.changed
    assert target.read_text(encoding="utf-8") == "one pin\ntwo pin\n"

    listed = registry.execute("glob", {"pattern": "**/*.txt"})
    assert listed.ok
    assert "notes/sample.txt" in listed.content
    searched = registry.execute("grep", {"pattern": r"one\s+pin", "file_pattern": "*.txt"})
    assert searched.ok
    assert "notes/sample.txt:1" in searched.content


def test_todo_skill_and_task_behavior_is_honest(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["todowrite", "skill", "task"],
        skill_instructions=[
            {
                "id": "review",
                "name": "Review skill",
                "description": "Review a patch before delivery.",
                "content": "Read the diff, test the changed path, then report evidence.",
            }
        ],
    )
    todos = registry.execute(
        "todowrite",
        {
            "todos": [
                {"id": "inspect", "content": "Inspect the code", "status": "completed"},
                {"id": "test", "content": "Run tests", "status": "in_progress"},
            ]
        },
    )
    assert todos.ok
    assert registry.runtime_state()["todo_state"][1]["status"] == "in_progress"
    skill = registry.execute("skill", {"skill_id": "review"})
    assert skill.ok
    assert "Read the diff" in skill.content
    assert registry.execute("skill", {"skill_id": "not-selected"}).error_code == "skill_not_selected"
    task = registry.execute("task", {"task": "delegate this", "agent_id": "child-agent"})
    assert not task.ok
    assert task.error_code == "delegated_task_unavailable"


@pytest.mark.asyncio
async def test_task_delegate_is_not_started_until_parent_approval(tmp_path) -> None:
    invoked: list[tuple[str, str, str | None]] = []

    async def delegate(task: str, *, agent_id: str, call_id: str | None = None):  # type: ignore[no-untyped-def]
        invoked.append((task, agent_id, call_id))
        from app.tools.types import ToolResult

        return ToolResult("task", True, "child done")

    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["task"],
        permission_mode="smart",
        task_delegate=delegate,
    )
    pending = await registry.execute_async(
        "task",
        {"task": "inspect", "agent_id": "child-1"},
        approved=False,
        call_id="parent-call-1",
    )
    assert pending.approval_required
    assert invoked == []

    approved = await registry.execute_async(
        "task",
        {"task": "inspect", "agent_id": "child-1"},
        approved=True,
        call_id="parent-call-1",
    )
    assert approved.ok and approved.content == "child done"
    assert invoked == [("inspect", "child-1", "parent-call-1")]


@pytest.mark.asyncio
async def test_question_finishes_the_current_run_and_emits_timeline_events(tmp_path) -> None:
    async def model_call(**_kwargs):
        return ModelTurn(tool_calls=[ModelToolCall("question-1", "question", {"question": "目标文件是哪一个？"})])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=["question"]),
    )
    outcome = await runtime.run(system_prompt="", recent_messages=[])

    assert outcome.status == "completed"
    assert outcome.output == "我需要先确认：目标文件是哪一个？"
    assert [event["type"] for event in outcome.events][-3:] == [
        "tool_finished",
        "user_question_requested",
        "run_completed",
    ]
    started = next(event for event in outcome.events if event["type"] == "model_step_started")
    tool_started = next(event for event in outcome.events if event["type"] == "tool_started")
    assert isinstance(started["monotonic_ms"], int)
    assert tool_started["tool_name"] == "question"
    assert "thought_duration_ms" in tool_started


@pytest.mark.asyncio
async def test_timeline_never_persists_write_content_or_api_key(tmp_path) -> None:
    async def model_call(**_kwargs):
        return ModelTurn(
            tool_calls=[
                ModelToolCall(
                    "write-1",
                    "write",
                    {"path": "safe.txt", "content": "TOP SECRET BODY", "api_key": "super-secret-key"},
                )
            ]
        )

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(
            str(tmp_path),
            allowed_tool_names=["write"],
            permission_mode="ask",
        ),
    )
    outcome = await runtime.run(system_prompt="", recent_messages=[])

    assert outcome.status == "awaiting_approval"
    started = next(event for event in outcome.events if event["type"] == "tool_started")
    assert started["arguments"]["content"] == "[redacted]"
    assert started["arguments"]["api_key"] == "[redacted]"
    assert "TOP SECRET BODY" not in str(started)
    assert "super-secret-key" not in str(started)
    approval_event = next(event for event in outcome.events if event["type"] == "approval_requested")
    assert "TOP SECRET BODY" not in str(approval_event)
    assert "super-secret-key" not in str(approval_event)
