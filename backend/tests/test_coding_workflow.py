from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

from src.coding import patch as patch_module
from src.coding import validation_runtime as validation_runtime_module
from src.coding.profiles import resolve_workflow_profile
from src.tools import create_default_registry
from src.tools.types import ToolResult


def _init_git_repository(path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "pgagent-test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "PGAgent Test"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "baseline"], cwd=path, check=True)


def test_apply_patch_updates_multiple_files_and_records_change_evidence(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_bytes(b"value = 1\r\n")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["apply_patch", "validate", "ToolSearch", "read", "git_diff"],
        permission_mode="smart",
    )

    result = registry.execute("apply_patch", {"patch": """*** Begin Patch
*** Update File: src/app.py
@@
-value = 1
+value = 2
*** Add File: src/helper.py
+def helper():
+    return 2
*** End Patch"""})

    assert result.ok and result.changed
    assert (tmp_path / "src" / "app.py").read_bytes() == b"value = 2\r\n"
    assert (tmp_path / "src" / "helper.py").read_text(encoding="utf-8") == "def helper():\n    return 2\n"
    state = registry.runtime_state()["coding_state"]
    assert [item["path"] for item in state["changes"][0]["files"]] == ["src/app.py", "src/helper.py"]
    assert "Validation status: not_run" in registry.workflow_prompt


def test_apply_patch_validates_every_hunk_before_writing_any_file(tmp_path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("before\n", encoding="utf-8")
    second.write_text("actual\n", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["apply_patch"],
        permission_mode="full",
    )

    result = registry.execute("apply_patch", {"patch": """*** Begin Patch
*** Update File: first.txt
@@
-before
+after
*** Update File: second.txt
@@
-missing
+changed
*** End Patch"""})

    assert not result.ok
    assert result.error_code == "patch_context_not_found"
    assert first.read_text(encoding="utf-8") == "before\n"
    assert second.read_text(encoding="utf-8") == "actual\n"


def test_apply_patch_rolls_back_an_earlier_file_when_a_later_commit_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first-before\n", encoding="utf-8")
    second.write_text("second-before\n", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["apply_patch"],
        permission_mode="full",
    )
    real_replace = patch_module.os.replace

    def fail_second_commit(source, target):  # type: ignore[no-untyped-def]
        if str(target).endswith("second.txt"):
            raise PermissionError("second target is read-only")
        return real_replace(source, target)

    monkeypatch.setattr(patch_module.os, "replace", fail_second_commit)
    result = registry.execute("apply_patch", {"patch": """*** Begin Patch
*** Update File: first.txt
@@
-first-before
+first-after
*** Update File: second.txt
@@
-second-before
+second-after
*** End Patch"""})

    assert not result.ok
    assert result.error_code == "patch_write_error"
    assert result.changed is False
    assert first.read_text(encoding="utf-8") == "first-before\n"
    assert second.read_text(encoding="utf-8") == "second-before\n"
    assert list(tmp_path.glob(".pgagent-patch-*")) == []


def test_smart_patch_policy_only_escalates_irreversible_or_sensitive_boundaries(tmp_path) -> None:
    (tmp_path / "obsolete.txt").write_text("old\n", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["apply_patch"],
        permission_mode="smart",
    )

    pending = registry.execute("apply_patch", {"patch": """*** Begin Patch
*** Delete File: obsolete.txt
*** End Patch"""})

    assert pending.approval_required
    assert (tmp_path / "obsolete.txt").exists()

    aliased_release_path = registry.execute("apply_patch", {"patch": """*** Begin Patch
*** Add File: tmp/../.github/workflows/release.yml
+jobs: {}
*** End Patch"""})
    assert aliased_release_path.approval_required

    reentered_release_path = registry.execute("apply_patch", {"patch": f"""*** Begin Patch
*** Add File: tmp/../../{tmp_path.name}/.github/workflows/release.yml
+jobs: {{}}
*** End Patch"""})
    assert reentered_release_path.approval_required


def test_read_uses_real_line_ranges_without_reading_only_a_prefix(tmp_path) -> None:
    (tmp_path / "long.txt").write_text("".join(f"line-{index}\n" for index in range(200)), encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read"],
        permission_mode="full",
    )

    result = registry.execute("read", {"path": "long.txt", "offset": 150, "limit": 2})

    assert result.ok
    assert result.content == "line-150\nline-151\n"
    assert result.metadata["offset"] == 150
    assert result.metadata["lines_returned"] == 2
    assert result.metadata["truncated"] is True


def test_rg_is_a_first_class_bounded_review_tool(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text(
        "def review_target():\n    return True\n",
        encoding="utf-8",
    )
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["rg"],
        permission_mode="full",
    )

    result = registry.execute(
        "rg",
        {"pattern": "review_target", "path": "src", "glob": "*.py"},
    )

    assert result.ok
    assert "src/service.py:1:def review_target" in result.content.replace("\\", "/")


def test_validate_runs_in_sandboxed_cwd_and_persists_result(tmp_path) -> None:
    (tmp_path / "backend").mkdir()
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["validate"],
        permission_mode="smart",
    )

    result = registry.execute(
        "validate",
        {"command": ["python", "--version"], "kind": "other", "cwd": "backend"},
    )

    assert result.ok
    assert result.metadata["validation"]["status"] == "passed"
    assert result.metadata["validation"]["cwd"] == "backend"
    state = registry.runtime_state()["coding_state"]
    assert state["validations"][0]["exit_code"] == 0


def test_validate_uses_frozen_docker_runtime_without_exposing_docker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "backend").mkdir()
    captured: dict = {}

    def fake_run_command(_sandbox, command, **kwargs) -> ToolResult:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return ToolResult(
            "run_command",
            True,
            "1 passed",
            metadata={"exit_code": 0, "cwd": "."},
        )

    monkeypatch.setattr(validation_runtime_module.builtins, "run_command", fake_run_command)
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["validate"],
        permission_mode="full",
        validation_runtime={"kind": "docker", "image": "swebench/test-image:latest"},
    )

    result = registry.execute(
        "validate",
        {
            "command": ["python", "-c", "print('a;b<c>d')"],
            "kind": "test",
            "cwd": "backend",
        },
    )

    assert result.ok
    assert captured["command"][:4] == [
        "docker", "run", "--rm", "--name",
    ]
    assert captured["command"][4].startswith("pgagent-validation-")
    assert "swebench/test-image:latest" in captured["command"]
    assert captured["command"][10].endswith(":/testbed")
    assert captured["command"][12] == "/testbed/backend"
    assert captured["command"][-3:] == ["python", "-c", "print('a;b<c>d')"]
    assert captured["kwargs"]["allowlist"] == frozenset({"docker", "docker.exe"})
    assert result.metadata["validation_runtime"] == "docker"
    assert registry.runtime_state()["validation_runtime"] == {
        "kind": "docker",
        "image": "swebench/test-image:latest",
    }


def test_docker_validation_reports_cleanup_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fake_run_command(_sandbox, _command, **_kwargs) -> ToolResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ToolResult("run_command", False, "timed out", error_code="timeout")
        return ToolResult("run_command", False, "daemon unavailable", error_code="command_error")

    monkeypatch.setattr(validation_runtime_module.builtins, "run_command", fake_run_command)
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["validate"],
        permission_mode="full",
        validation_runtime={"kind": "docker", "image": "swebench/test:latest"},
    )

    result = registry.execute("validate", {"command": ["python", "--version"]})

    assert not result.ok
    assert result.metadata["docker_cleanup_succeeded"] is False
    assert "Docker 容器清理失败" in result.content


def test_change_after_validation_marks_the_evidence_stale(tmp_path) -> None:
    (tmp_path / "value.txt").write_text("before\n", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["apply_patch", "validate"],
        permission_mode="full",
    )

    validated = registry.execute(
        "validate",
        {"command": ["python", "--version"], "kind": "other"},
    )
    changed = registry.execute("apply_patch", {"patch": """*** Begin Patch
*** Update File: value.txt
@@
-before
+after
*** End Patch"""})

    assert validated.ok and changed.ok
    assert "Validation status: stale" in registry.workflow_prompt


def test_coding_bash_detects_real_tracked_file_changes(tmp_path) -> None:
    source = tmp_path / "value.txt"
    source.write_text("before\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["bash", "apply_patch", "validate"],
        workflow_profile_id="coding",
        permission_mode="full",
    )

    result = registry.execute(
        "bash",
        {
            "command": [
                "python",
                "-c",
                "__import__('pathlib').Path('value.txt').write_text('after\\n', encoding='utf-8')",
            ]
        },
    )

    assert result.ok and result.changed
    assert result.metadata["change_set"]["source"] == "shell"
    assert result.metadata["change_set"]["files"] == [
        {"path": "value.txt", "operation": "update"}
    ]
    state = registry.runtime_state()["coding_state"]
    assert state["changes"][-1]["tool"] == "bash"
    assert "Changed paths: value.txt" in registry.workflow_prompt


def test_coding_bash_detects_existing_untracked_file_content_changes(tmp_path) -> None:
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    scratch = tmp_path / "scratch.txt"
    scratch.write_text("v1\n", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["bash", "apply_patch", "validate"],
        workflow_profile_id="coding",
        permission_mode="full",
    )
    validated = registry.execute(
        "validate",
        {"command": ["python", "--version"], "kind": "other"},
    )

    result = registry.execute(
        "bash",
        {
            "command": [
                "python",
                "-c",
                "__import__('pathlib').Path('scratch.txt').write_text('version-two\\n', encoding='utf-8')",
            ]
        },
    )

    assert validated.ok
    assert result.ok and result.changed
    assert result.metadata["change_set"]["files"] == [
        {"path": "scratch.txt", "operation": "add"}
    ]
    assert "Validation status: stale" in registry.workflow_prompt


def test_coding_bash_reports_only_files_changed_by_this_command(tmp_path) -> None:
    user_file = tmp_path / "user.txt"
    agent_file = tmp_path / "agent.txt"
    user_file.write_text("baseline\n", encoding="utf-8")
    agent_file.write_text("baseline\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    user_file.write_text("preexisting user change\n", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["bash", "apply_patch", "validate"],
        workflow_profile_id="coding",
        permission_mode="full",
    )

    result = registry.execute(
        "bash",
        {
            "command": [
                "python",
                "-c",
                "__import__('pathlib').Path('agent.txt').write_text('agent change\\n', encoding='utf-8')",
            ]
        },
    )

    assert result.ok and result.changed
    assert result.metadata["change_set"]["files"] == [
        {"path": "agent.txt", "operation": "update"}
    ]


def test_coding_bash_detects_same_size_untracked_change_with_restored_mtime(tmp_path) -> None:
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    scratch = tmp_path / "scratch.txt"
    scratch.write_text("one\n", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["bash", "apply_patch", "validate"],
        workflow_profile_id="coding",
        permission_mode="full",
    )

    result = registry.execute(
        "bash",
        {
            "command": [
                "python",
                "-c",
                "exec(\"from pathlib import Path\\nimport os\\np = Path('scratch.txt')\\ns = p.stat()\\np.write_text('two\\\\n', encoding='utf-8')\\nos.utime(p, ns=(s.st_atime_ns, s.st_mtime_ns))\")",
            ]
        },
    )

    assert result.ok and result.changed
    assert result.metadata["change_set"]["files"] == [
        {"path": "scratch.txt", "operation": "add"}
    ]


@pytest.mark.parametrize(
    ("file_name", "tracked", "operation"),
    [("代码.py", True, "update"), ("草稿.txt", False, "add")],
)
def test_coding_bash_reports_unicode_paths(
    tmp_path,
    file_name: str,
    tracked: bool,
    operation: str,
) -> None:
    (tmp_path / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    target = tmp_path / file_name
    if tracked:
        target.write_text("before\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    if not tracked:
        target.write_text("before\n", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["bash", "apply_patch", "validate"],
        workflow_profile_id="coding",
        permission_mode="full",
    )

    result = registry.execute(
        "bash",
        {
            "command": [
                "python",
                "-c",
                f"__import__('pathlib').Path({file_name!r}).write_text('after\\n', encoding='utf-8')",
            ]
        },
    )

    assert result.ok and result.changed
    assert result.metadata["change_set"]["files"] == [
        {"path": file_name, "operation": operation}
    ]


def test_validate_baseline_runs_in_isolated_head_worktree(tmp_path) -> None:
    source = tmp_path / "value.txt"
    source.write_text("baseline\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    source.write_text("candidate\n", encoding="utf-8")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["validate_baseline", "apply_patch", "validate"],
        workflow_profile_id="coding",
        permission_mode="full",
    )

    result = registry.execute(
        "validate_baseline",
        {
            "command": [
                "python",
                "-c",
                "assert __import__('pathlib').Path('value.txt').read_text(encoding='utf-8') == 'baseline\\n'",
            ],
            "kind": "test",
        },
    )

    assert result.ok
    assert result.metadata["baseline_validation"]["ref"] == "HEAD"
    assert result.metadata["baseline_validation"]["isolated"] is True
    assert source.read_text(encoding="utf-8") == "candidate\n"
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert worktrees.count("worktree ") == 1
    assert registry.runtime_state()["coding_state"]["validations"] == []


def test_validate_baseline_unlocks_and_removes_worktree_after_command(tmp_path) -> None:
    (tmp_path / "value.txt").write_text("baseline\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["validate_baseline", "apply_patch", "validate"],
        workflow_profile_id="coding",
        permission_mode="full",
    )

    result = registry.execute(
        "validate_baseline",
        {"command": ["git", "worktree", "lock", "--reason", "test-lock", "."]},
    )

    assert result.ok
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert worktrees.count("worktree ") == 1


@pytest.mark.asyncio
async def test_validate_baseline_cancellation_stops_command_and_removes_worktree(tmp_path) -> None:
    (tmp_path / "value.txt").write_text("baseline\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["validate_baseline", "apply_patch", "validate"],
        workflow_profile_id="coding",
        permission_mode="full",
    )

    pending = asyncio.create_task(registry.execute_async(
        "validate_baseline",
        {"command": ["python", "-c", "__import__('time').sleep(30)"]},
    ))
    await asyncio.sleep(0.5)
    registry.cancel_active()
    result = await asyncio.wait_for(pending, timeout=10)

    assert not result.ok
    assert result.error_code == "cancelled"
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert worktrees.count("worktree ") == 1


@pytest.mark.asyncio
async def test_validate_cancellation_stops_command(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["validate"],
        workflow_profile_id="coding",
        permission_mode="full",
    )

    pending = asyncio.create_task(registry.execute_async(
        "validate",
        {
            "kind": "test",
            "command": ["python", "-c", "__import__('time').sleep(30)"],
        },
    ))
    await asyncio.sleep(0.5)
    registry.cancel_active()
    result = await asyncio.wait_for(pending, timeout=10)

    assert not result.ok
    assert result.error_code == "cancelled"


def test_validate_baseline_does_not_prune_unrelated_missing_worktree(tmp_path) -> None:
    (tmp_path / "value.txt").write_text("baseline\n", encoding="utf-8")
    _init_git_repository(tmp_path)
    offline_worktree = tmp_path.parent / f"{tmp_path.name}-offline"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(offline_worktree), "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    shutil.rmtree(offline_worktree)
    subprocess.run(
        ["git", "config", "gc.worktreePruneExpire", "now"],
        cwd=tmp_path,
        check=True,
    )
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["validate_baseline", "apply_patch", "validate"],
        workflow_profile_id="coding",
        permission_mode="full",
    )

    result = registry.execute(
        "validate_baseline",
        {"command": ["python", "--version"]},
    )

    assert result.ok
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert worktrees.count("worktree ") == 2


@pytest.mark.asyncio
async def test_coding_profile_keeps_review_core_visible_and_activates_low_frequency_tools(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=[
            "ToolSearch", "apply_patch", "validate", "read", "grep", "git_diff", "GitBlame",
        ],
        permission_mode="full",
    )

    assert set(registry.model_visible_tool_names) == {
        "ToolSearch", "apply_patch", "validate", "read", "grep", "git_diff",
    }
    search = await registry.execute_async("ToolSearch", {"query": "select:GitBlame"})
    assert json.loads(search.content)[0]["name"] == "GitBlame"
    assert search.metadata["activated_tools"] == ["GitBlame"]
    assert "GitBlame" in registry.model_visible_tool_names
    snapshot = registry.runtime_state()
    assert snapshot["builtin_active_tools"] == ["GitBlame"]

    resumed = create_default_registry(
        str(tmp_path),
        allowed_tool_names=snapshot["allowed_tool_names"],
        permission_mode="full",
        coding_state=snapshot["coding_state"],
        active_builtin_tool_names=snapshot["builtin_active_tools"],
    )
    assert "GitBlame" in resumed.model_visible_tool_names


def test_coding_profile_does_not_defer_tools_when_tool_search_is_unavailable(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["apply_patch", "write"],
        permission_mode="full",
    )

    assert registry.model_visible_tool_names == ("apply_patch", "write")
    assert registry.workflow_prompt == ""


@pytest.mark.asyncio
async def test_explicit_review_profile_records_and_restores_structured_findings(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=[
            "ToolSearch", "read", "rg", "git_diff", "validate", "review_finding",
            "apply_patch", "MCP", "TeamCreate", "read_inbox",
        ],
        workflow_profile_id="review",
        permission_mode="full",
    )

    result = await registry.execute_async(
        "review_finding",
        {
            "severity": "important",
            "title": "Stale cache survives update",
            "path": "src/cache.py",
            "line": 42,
            "failure_scenario": "Updating an existing key returns the previous value.",
            "evidence": "update() writes storage but does not invalidate _cached.",
            "suggested_fix": "Invalidate the entry after the storage write.",
        },
        call_id="review-call-1",
    )

    assert result.ok
    snapshot = registry.runtime_state()
    assert snapshot["workflow_profile_id"] == "review"
    assert snapshot["workflow_evidence_state"]["review_findings"][0]["call_id"] == "review-call-1"
    assert "[important] Stale cache survives update (src/cache.py:42)" in registry.workflow_prompt
    assert "Failure scenario: Updating an existing key returns the previous value." in registry.workflow_prompt
    assert "Evidence: update() writes storage but does not invalidate _cached." in registry.workflow_prompt
    assert "apply_patch" not in registry.model_visible_tool_names
    assert {"MCP", "TeamCreate", "read_inbox"}.isdisjoint(registry.model_visible_tool_names)
    search = await registry.execute_async("ToolSearch", {"query": "select:apply_patch"})
    assert json.loads(search.content) == []
    assert search.metadata["activated_tools"] == []

    async def external(arguments: dict) -> ToolResult:
        return ToolResult("external", True, json.dumps(arguments))

    schema = {
        "description": "test integration",
        "parameters": {"type": "object", "properties": {}},
    }
    registry.register_external(
        "mcp__repo__mutate",
        schema,
        external,
        read_only=False,
        parallel=False,
        exposure="deferred",
        owner="mcp:repo",
    )
    registry.register_external(
        "mcp__repo__inspect",
        schema,
        external,
        read_only=True,
        parallel=True,
        exposure="deferred",
        owner="mcp:repo",
    )
    hidden = await registry.execute_async("ToolSearch", {"query": "select:mcp__repo__mutate"})
    assert json.loads(hidden.content) == []
    visible = await registry.execute_async("ToolSearch", {"query": "select:mcp__repo__inspect"})
    assert visible.metadata["activated_tools"] == ["mcp__repo__inspect"]
    assert "mcp__repo__inspect" in registry.model_visible_tool_names

    resumed = create_default_registry(
        str(tmp_path),
        allowed_tool_names=snapshot["allowed_tool_names"],
        workflow_profile_id=snapshot["workflow_profile_id"],
        workflow_evidence_state=snapshot["workflow_evidence_state"],
        permission_mode="full",
    )
    assert "Recorded review findings" in resumed.workflow_prompt
    assert resumed.runtime_state()["workflow_evidence_state"] == snapshot["workflow_evidence_state"]


def test_explicit_debug_profile_keeps_hypotheses_distinct_from_root_cause(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read", "rg", "debug_evidence", "apply_patch", "validate"],
        workflow_profile_id="debug",
        permission_mode="full",
    )

    hypothesis = registry.execute(
        "debug_evidence",
        {
            "stage": "hypothesis",
            "summary": "The parser may reuse a stale token buffer.",
            "status": "unconfirmed",
            "path": "src/parser.py",
        },
    )
    root_cause = registry.execute(
        "debug_evidence",
        {
            "stage": "root_cause",
            "summary": "reset() does not clear the token buffer.",
            "status": "confirmed",
            "path": "src/parser.py",
            "line": 18,
        },
    )

    assert hypothesis.ok and root_cause.ok
    state = registry.runtime_state()["workflow_evidence_state"]["debug_evidence"]
    assert [(item["stage"], item["status"]) for item in state] == [
        ("hypothesis", "unconfirmed"),
        ("root_cause", "confirmed"),
    ]
    assert "hypothesis status=unconfirmed" in registry.workflow_prompt
    assert "root_cause status=confirmed" in registry.workflow_prompt
    assert "Location: src/parser.py:18" in registry.workflow_prompt


def test_workflow_profile_resolution_preserves_auto_compatibility() -> None:
    assert resolve_workflow_profile("review", ["read"]).id == "review"
    assert resolve_workflow_profile("debug", ["read"]).id == "debug"
    assert resolve_workflow_profile("general", ["apply_patch", "validate"]) is None
    assert resolve_workflow_profile("auto", ["apply_patch", "validate"]).id == "coding"
    assert resolve_workflow_profile("auto", ["read", "rg"]) is None
