from __future__ import annotations

import json

import pytest

from src.coding import patch as patch_module
from src.coding.profiles import resolve_workflow_profile
from src.tools import create_default_registry
from src.tools.types import ToolResult


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
