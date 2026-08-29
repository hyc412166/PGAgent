from __future__ import annotations

import json

import pytest

from src.context.window import ContextManager
from src.context.assembly import COMPACTION_SECTION_TITLES, ContextAssembler, ConversationCompactor
from src.agent.engine import AgentRuntime, ModelToolCall, ModelTurn, RuntimeConfig
from src.tools import create_default_registry
from src.tools.policy import assess_tool_call


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
    # Smart mode confirms the external boundary first.  Approval only removes
    # that prompt; the tool still enforces its own SSRF boundary afterwards.
    pending = smart_registry.execute("webfetch", {"url": "http://127.0.0.1:8765"})
    assert pending.approval_required
    blocked = smart_registry.execute(
        "webfetch",
        {"url": "http://127.0.0.1:8765"},
        approved=True,
    )
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


def test_smart_mode_allows_routine_code_edits_but_escalates_sensitive_or_broad_writes(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "src.py").write_text("print('before')\n", encoding="utf-8")
    (tmp_path / "large.txt").write_text("x" * 70_000, encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["write", "edit"],
        permission_mode="smart",
    )

    routine_edit = registry.execute(
        "edit",
        {"path": "src/src.py", "old_string": "before", "new_string": "after"},
    )
    assert routine_edit.ok
    assert (tmp_path / "src" / "src.py").read_text(encoding="utf-8") == "print('after')\n"

    routine_write = registry.execute("write", {"path": "src/new.py", "content": "print('ok')\n"})
    assert routine_write.ok

    sensitive = registry.execute("write", {"path": ".env", "content": "API_KEY=demo"})
    assert sensitive.approval_required
    assert "环境变量" in sensitive.content

    control_plane = registry.execute("write", {"path": ".github/workflows/release.yml", "content": "jobs: {}"})
    assert control_plane.approval_required
    assert "自动化" in control_plane.content

    destructive = registry.execute("write", {"path": "large.txt", "content": "short"})
    assert destructive.approval_required
    assert "显著缩小" in destructive.content


def test_smart_mode_classifies_commands_by_exact_arguments(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["bash"],
        permission_mode="smart",
    )

    # A deterministic project check does not pause for approval. It may fail on
    # an empty directory, which is unrelated to the approval decision.
    pytest_check = registry.execute("bash", {"command": ["pytest", "--version"]})
    assert not pytest_check.approval_required

    git_status = assess_tool_call(
        "bash",
        "smart",
        arguments={"command": ["git", "status", "--short"]},
        approved=False,
        workspace_root=tmp_path,
    )
    assert not git_status.requires_approval

    for command, clue in (
        (["git", "reset", "--hard"], "Git"),
        (["npm", "install"], "npm"),
        (["python", "-c", "print('arbitrary code')"], "Python"),
        ("pytest && git status", "shell 控制符"),
    ):
        pending = registry.execute("bash", {"command": command})
        assert pending.approval_required
        assert clue in pending.content


def test_permission_modes_keep_their_distinct_boundaries_for_the_same_write(tmp_path) -> None:
    arguments = {"path": "src/src.py", "content": "print('ok')\n"}

    ask = create_default_registry(str(tmp_path), allowed_tool_names=["write"], permission_mode="ask")
    assert ask.execute("write", arguments).approval_required

    smart = create_default_registry(str(tmp_path), allowed_tool_names=["write"], permission_mode="smart")
    assert smart.execute("write", arguments).ok

    full = create_default_registry(str(tmp_path), allowed_tool_names=["write"], permission_mode="full")
    assert full.execute("write", {"path": ".env", "content": "API_KEY=demo"}).ok


def test_delete_is_public_and_smart_mode_always_requires_approval(tmp_path) -> None:
    target = tmp_path / "obsolete.txt"
    target.write_text("old", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["delete"],
        permission_mode="smart",
    )

    assert [item["function"]["name"] for item in registry.schemas] == ["delete"]
    pending = registry.execute("delete", {"path": "obsolete.txt"})
    assert pending.approval_required
    assert target.exists()

    approved = registry.execute("delete", {"path": "obsolete.txt"}, approved=True)
    assert approved.ok and approved.changed
    assert not target.exists()


def test_malformed_provider_tool_arguments_are_rejected_without_execution(tmp_path) -> None:
    turn = ModelTurn.from_response({
        "tool_calls": [{
            "id": "bad-json",
            "function": {"name": "write", "arguments": '{"path":"x.txt","content":'},
        }]
    })
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["write"],
        permission_mode="full",
    )

    result = registry.execute("write", turn.tool_calls[0].arguments)
    assert not result.ok
    assert result.error_code == "invalid_tool_arguments"
    assert not (tmp_path / "x.txt").exists()
    assert "content" not in result.content


def test_model_turn_preserves_provider_reasoning_for_followup_requests() -> None:
    turn = ModelTurn.from_response({
        "choices": [{"message": {
            "role": "assistant",
            "content": "",
            "reasoning_content": "inspect the project first",
            "tool_calls": [],
        }}],
        "usage": {"request_count": 1},
    })

    assert turn.reasoning_content == "inspect the project first"
    assert turn.usage["request_count"] == 1


@pytest.mark.asyncio
async def test_runtime_never_persists_raw_malformed_tool_arguments(tmp_path) -> None:
    calls = 0
    malformed = '{"path":"x.txt","content":"DO_NOT_PERSIST"'

    async def model_call(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "tool_calls": [{
                    "id": "bad-json-runtime",
                    "function": {"name": "write", "arguments": malformed},
                }]
            }
        assert kwargs["messages"][-1]["role"] == "tool"
        assert "invalid_tool_arguments" in kwargs["messages"][-1]["content"]
        return ModelTurn(content="参数无效，未执行写入。")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(
            str(tmp_path),
            allowed_tool_names=["write"],
            permission_mode="full",
        ),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "completed"
    assert not (tmp_path / "x.txt").exists()
    serialized = json.dumps({"messages": outcome.messages, "events": outcome.events}, ensure_ascii=False)
    assert "DO_NOT_PERSIST" not in serialized
    assert "_raw" not in serialized


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
        permission_mode="full",
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
    missing_id = registry.execute(
        "todowrite",
        {"todos": [{"content": "Unstable step", "status": "pending"}]},
    )
    assert not missing_id.ok and missing_id.error_code == "invalid_todos"
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
        from src.tools.types import ToolResult

        return ToolResult("task", True, "child done")

    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["task"],
        permission_mode="ask",
        task_delegate=delegate,
    )
    invalid_target = await registry.execute_async(
        "task",
        {"task": "inspect", "agent_id": ""},
    )
    assert invalid_target.error_code == "invalid_delegate_agent"
    assert invoked == []

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
async def test_async_command_approval_does_not_persist_private_cancel_signal(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["bash"],
        permission_mode="ask",
    )
    pending = await registry.execute_async("bash", {"command": "echo safe"}, call_id="approval-1")
    assert pending.approval_required
    assert pending.approval_request is not None
    assert "_cancel_event" not in pending.approval_request.arguments
    json.dumps(pending.approval_request.arguments)


@pytest.mark.asyncio
async def test_question_stops_for_input_without_claiming_completion(tmp_path) -> None:
    async def model_call(**_kwargs):
        return ModelTurn(tool_calls=[ModelToolCall("question-1", "question", {"question": "目标文件是哪一个？"})])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=["question"]),
    )
    outcome = await runtime.run(system_prompt="", recent_messages=[])

    assert outcome.status == "stopped"
    assert outcome.stop_reason == "needs_user_input"
    assert outcome.output == "我需要先确认：目标文件是哪一个？"
    assert [event["type"] for event in outcome.events][-3:] == [
        "tool_finished",
        "user_question_requested",
        "run_stopped",
    ]
    started = next(event for event in outcome.events if event["type"] == "model_step_started")
    tool_started = next(event for event in outcome.events if event["type"] == "tool_started")
    assert isinstance(started["monotonic_ms"], int)
    assert tool_started["tool_name"] == "question"
    assert "thought_duration_ms" in tool_started


@pytest.mark.asyncio
async def test_provider_context_overflow_compacts_once_and_retries_without_loop(tmp_path) -> None:
    class ContextOverflow(RuntimeError):
        status_code = 400

    calls: list[str] = []

    async def model_call(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(kwargs.get("mode")))
        if kwargs.get("mode") == "auto" and calls.count("auto") == 1:
            raise ContextOverflow("maximum context length exceeded")
        return ModelTurn(content="recovered")

    assembler = ContextAssembler(
        max_tokens=4_096,
        compaction_threshold_tokens=3_000,
        output_reserve_tokens=500,
        safety_buffer_tokens=100,
    )
    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        context_manager=ContextManager(max_tokens=4_096),
        context_assembler=assembler,
        conversation_compactor=ConversationCompactor(
            model_call=model_call,
            preserve_recent_messages=1,
            retain_tokens=100,
        ),
    )
    outcome = await runtime.run(
        system_prompt="rules",
        agent_instructions="instructions",
        recent_messages=[
            {"role": "user", "content": "old " + ("x" * 8_000)},
            {"role": "assistant", "content": "old result"},
            {"role": "user", "content": "recover now"},
            {"role": "assistant", "content": "latest small result"},
        ],
    )

    assert outcome.status == "completed"
    assert outcome.output == "recovered"
    assert calls.count("auto") == 2
    overflow_events = [
        event for event in outcome.events
        if event.get("reason") == "provider_context_overflow"
    ]
    assert [event["type"] for event in overflow_events] == [
        "context_compaction_started",
        "context_compaction_finished",
    ]


@pytest.mark.asyncio
async def test_threshold_full_compaction_builds_exact_continuation(tmp_path) -> None:
    calls: list[str] = []
    model_messages: list[dict] = []

    async def model_call(**kwargs):  # type: ignore[no-untyped-def]
        mode = str(kwargs.get("mode") or "auto")
        calls.append(mode)
        if mode == "compaction":
            return ModelTurn(
                content="\n".join(
                    f"## {index}. {title}\nPreserved continuation fact {index}."
                    for index, title in enumerate(COMPACTION_SECTION_TITLES, start=1)
                )
            )
        model_messages.extend(kwargs["messages"])
        return ModelTurn(content="compacted answer")

    assembler = ContextAssembler(
        max_tokens=4_096,
        compaction_threshold_tokens=1_800,
        output_reserve_tokens=500,
        safety_buffer_tokens=100,
    )
    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        context_manager=ContextManager(max_tokens=4_096),
        context_assembler=assembler,
        conversation_compactor=ConversationCompactor(
            model_call=model_call,
            preserve_recent_messages=1,
            retain_tokens=100,
        ),
    )
    outcome = await runtime.run(
        system_prompt="stable rules",
        agent_instructions="stable instructions",
        recent_messages=[
            {"role": "user", "content": "old context " + ("x" * 4_000)},
            {"role": "user", "content": "important task"},
            {"role": "assistant", "content": "intermediate result " + ("y" * 4_000)},
        ],
    )

    assert outcome.status == "completed"
    assert outcome.output == "compacted answer"
    assert "compaction" in calls
    rendered = "\n".join(str(item.get("content") or "") for item in model_messages)
    assert "<continuation-summary" in rendered
    assert "Authoritative current task facts" in rendered
    assert "important task" in rendered
    assert any(event["type"] == "context_compaction_finished" and event.get("effective") for event in outcome.events)


@pytest.mark.asyncio
async def test_aggregate_tool_results_are_budgeted_before_nine_section_compaction(tmp_path) -> None:
    calls: list[str] = []
    provider_messages: list[dict] = []

    async def model_call(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(kwargs.get("mode") or "auto"))
        if kwargs.get("mode") == "auto":
            provider_messages.extend(kwargs["messages"])
        return ModelTurn(content="done")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        context_manager=ContextManager(max_tokens=1_000_000),
        context_assembler=ContextAssembler(
            max_tokens=1_000_000,
            compaction_threshold_tokens=150_000,
            output_reserve_tokens=0,
            safety_buffer_tokens=0,
        ),
    )
    outcome = await runtime.run(
        system_prompt="stable rules",
        recent_messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "large-1", "function": {"name": "read", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "large-1", "name": "read", "content": "a" * 200_000},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "large-2", "function": {"name": "read", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "large-2", "name": "read", "content": "b" * 110_001},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "recent-1", "function": {"name": "read", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "recent-1", "name": "read", "content": "recent-one"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "recent-2", "function": {"name": "read", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "recent-2", "name": "read", "content": "recent-two"},
            {"role": "user", "content": "continue"},
        ],
    )

    assert outcome.status == "completed"
    assert calls == ["auto"]
    tool_results = [item for item in provider_messages if item.get("role") == "tool"]
    assert sum(len(str(item.get("content") or "")) for item in tool_results) <= 150_000
    assert str(tool_results[0]["content"]).startswith("<persisted-tool-output>")
    assert tool_results[1]["content"] == "b" * 110_001
    assert [item["content"] for item in tool_results[-2:]] == ["recent-one", "recent-two"]
    assert len(outcome.artifact_refs) == 1


@pytest.mark.asyncio
async def test_exhausted_tool_result_budget_passes_to_normal_threshold_logic(tmp_path) -> None:
    calls: list[str] = []

    async def model_call(**kwargs):  # type: ignore[no-untyped-def]
        mode = str(kwargs.get("mode") or "auto")
        calls.append(mode)
        if mode == "compaction":
            return ModelTurn(content="\n".join(
                f"## {index}. {title}\nPreserved fact {index}."
                for index, title in enumerate(COMPACTION_SECTION_TITLES, start=1)
            ))
        return ModelTurn(content="done")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        context_manager=ContextManager(max_tokens=1_000_000),
        context_assembler=ContextAssembler(
            max_tokens=1_000_000,
            compaction_threshold_tokens=900_000,
            output_reserve_tokens=0,
            safety_buffer_tokens=0,
        ),
    )
    messages: list[dict] = [{"role": "user", "content": "continue"}]
    for index in range(76):
        messages.extend([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"medium-{index}", "function": {"name": "read", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": f"medium-{index}",
                "name": "read",
                "content": "x" * 4_000,
            },
        ])

    outcome = await runtime.run(system_prompt="stable rules", recent_messages=messages)

    assert outcome.status == "completed"
    assert calls == ["auto"]
    finished = [event for event in outcome.events if event.get("type") == "context_compaction_finished"]
    assert finished == []
    assert {ref["kind"] for ref in outcome.artifact_refs} == {"tool_output"}


@pytest.mark.asyncio
async def test_task_checkpoint_failure_keeps_original_messages_instead_of_degraded_compaction(tmp_path) -> None:
    modes: list[str] = []

    async def model_call(**kwargs):  # type: ignore[no-untyped-def]
        modes.append(str(kwargs.get("mode") or "auto"))
        return ModelTurn(content="answer from original context")

    def unavailable_checkpoint():
        raise RuntimeError("database temporarily unavailable")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
        context_manager=ContextManager(max_tokens=4_096),
        context_assembler=ContextAssembler(
            max_tokens=4_096,
            compaction_threshold_tokens=300,
            output_reserve_tokens=0,
            safety_buffer_tokens=0,
        ),
        task_state_provider=unavailable_checkpoint,
    )
    outcome = await runtime.run(
        system_prompt="stable rules",
        recent_messages=[{"role": "user", "content": "keep this " + ("x" * 4_000)}],
    )

    assert outcome.status == "completed"
    assert modes == ["auto"]
    assert any(event["type"] == "context_compaction_failed" for event in outcome.events)
    assert all(not str(item.get("content") or "").startswith("<continuation-summary") for item in outcome.messages)


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
