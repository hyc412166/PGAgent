"""验证基础文件、搜索、命令和委派工具的沙箱边界、审批要求、超时与输出限制。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path


from src.tools.builtins import (
    delete_file,
    delegate_task_async,
    file_info,
    git_diff,
    git_status,
    list_files,
    read_file,
    run_command,
    search_files,
    write_file,
)
from src.tools.sandbox import SandboxViolation, WorkspaceSandbox
from src.tools.types import ToolResult


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_task_batch_runs_child_delegates_concurrently 精确标识本用例的具体条件。
def test_task_batch_runs_child_delegates_concurrently(tmp_path: Path) -> None:
    # 辅助方法：run 实现测试替身在此调用阶段需要的最小行为。
    async def run() -> None:
        active = 0
        max_active = 0

        # 辅助方法：delegate 实现测试替身在此调用阶段需要的最小行为。
        async def delegate(task: str, *, agent_id: str, call_id: str | None = None) -> ToolResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return ToolResult(
                "task",
                True,
                json.dumps({"task": task, "agent_id": agent_id, "call_id": call_id}),
            )

        result = await delegate_task_async(
            WorkspaceSandbox(tmp_path),
            tasks=[
                {"task": "research", "agent_id": "agent-a"},
                {"task": "review", "agent_id": "agent-b"},
            ],
            delegate=delegate,
            call_id="parallel-batch",
        )

        payload = json.loads(result.content)
        assert result.ok
        assert payload["parallel"] is True
        assert payload["child_count"] == 2
        assert max_active == 2

    asyncio.run(run())


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_task_batch_reports_partial_failure 精确标识本用例的具体条件。
def test_task_batch_reports_partial_failure(tmp_path: Path) -> None:
    # 辅助方法：run 实现测试替身在此调用阶段需要的最小行为。
    async def run() -> None:
        # 辅助方法：delegate 实现测试替身在此调用阶段需要的最小行为。
        async def delegate(task: str, *, agent_id: str, call_id: str | None = None) -> ToolResult:
            if agent_id == "agent-b":
                raise RuntimeError("child failed")
            return ToolResult("task", True, json.dumps({"status": "completed", "output": task}))

        result = await delegate_task_async(
            WorkspaceSandbox(tmp_path),
            tasks=[
                {"task": "research", "agent_id": "agent-a"},
                {"task": "review", "agent_id": "agent-b"},
            ],
            delegate=delegate,
            call_id="mixed-batch",
        )

        payload = json.loads(result.content)
        assert not result.ok
        assert result.error_code == "delegate_partial_failure"
        assert payload["status"] == "partial_failure"
        assert payload["completed_count"] == 1
        assert payload["failed_count"] == 1
        assert [child["ok"] for child in payload["children"]] == [True, False]

    asyncio.run(run())


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_sandbox_rejects_parent_and_absolute_paths 精确标识本用例的具体条件。
def test_sandbox_rejects_parent_and_absolute_paths(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)

    for unsafe in ("../outside.txt", str((tmp_path.parent / "outside.txt").resolve())):
        try:
            sandbox.resolve(unsafe)
        except SandboxViolation:
            pass
        else:
            raise AssertionError(f"unsafe path was accepted: {unsafe}")


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_read_file_cannot_escape_workspace 精确标识本用例的具体条件。
def test_read_file_cannot_escape_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "pgagent-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    result = read_file(WorkspaceSandbox(tmp_path), "../pgagent-outside.txt")
    assert not result.ok
    assert result.error_code == "path_error"
    assert "secret" not in result.content


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_read_file_rejects_symlink_that_points_outside 精确标识本用例的具体条件。
def test_read_file_rejects_symlink_that_points_outside(tmp_path: Path) -> None:
    outside = tmp_path.parent / "pgagent-symlink-target.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        # Some Windows configurations disallow unprivileged symlinks.
        return
    result = read_file(WorkspaceSandbox(tmp_path), "link.txt")
    assert not result.ok
    assert "secret" not in result.content


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_search_files_skips_symlink_that_points_outside 精确标识本用例的具体条件。
def test_search_files_skips_symlink_that_points_outside(tmp_path: Path) -> None:
    outside = tmp_path.parent / "pgagent-search-target.txt"
    outside.write_text("DO_NOT_LEAK", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    result = search_files(WorkspaceSandbox(tmp_path), "DO_NOT_LEAK")
    assert result.ok
    assert "DO_NOT_LEAK" not in result.content
    assert result.metadata["skipped"] >= 1


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_recursive_listing_does_not_follow_outward_directory_link 精确标识本用例的具体条件。
def test_recursive_listing_does_not_follow_outward_directory_link(tmp_path: Path) -> None:
    outside = tmp_path.parent / "pgagent-junction-target"
    outside.mkdir(exist_ok=True)
    (outside / "SECRET_NAME.txt").write_text("secret", encoding="utf-8")
    link = tmp_path / "linked"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            return
    else:
        link.symlink_to(outside, target_is_directory=True)

    result = list_files(WorkspaceSandbox(tmp_path), recursive=True)
    assert result.ok
    assert "SECRET_NAME.txt" not in result.content
    assert "linked" not in result.content

    search_result = search_files(WorkspaceSandbox(tmp_path), "secret")
    assert search_result.ok
    assert "SECRET_NAME.txt" not in search_result.content


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_read_file_streams_only_requested_character_limit 精确标识本用例的具体条件。
def test_read_file_streams_only_requested_character_limit(tmp_path: Path) -> None:
    large = tmp_path / "large.txt"
    large.write_text("z" * 2_000_000, encoding="utf-8")
    result = read_file(WorkspaceSandbox(tmp_path), "large.txt", max_chars=100)
    assert result.ok
    assert len(result.content) == 100
    assert result.metadata["truncated"] is True
    assert result.metadata["size_bytes"] == 2_000_000


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_read_file_does_not_mark_complete_crlf_text_truncated 精确标识本用例的具体条件。
def test_read_file_does_not_mark_complete_crlf_text_truncated(tmp_path: Path) -> None:
    (tmp_path / "windows.txt").write_bytes(b"first\r\nsecond\r\n")
    result = read_file(WorkspaceSandbox(tmp_path), "windows.txt", max_chars=100)
    assert result.ok
    assert result.metadata["truncated"] is False


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_search_reports_when_scan_limit_was_reached 精确标识本用例的具体条件。
def test_search_reports_when_scan_limit_was_reached(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("needle" if name == "c.txt" else "other", encoding="utf-8")
    result = search_files(WorkspaceSandbox(tmp_path), "needle", max_entries=2)
    assert result.ok
    assert result.metadata["truncated"] is True
    assert result.metadata["visited"] == 2


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_write_requires_approval_then_writes 精确标识本用例的具体条件。
def test_write_requires_approval_then_writes(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    pending = write_file(sandbox, "notes/result.txt", "hello")
    assert pending.approval_required
    assert pending.approval_request is not None
    assert not (tmp_path / "notes" / "result.txt").exists()

    result = write_file(sandbox, "notes/result.txt", "hello", approved=True)
    assert result.ok and result.changed
    assert (tmp_path / "notes" / "result.txt").read_text(encoding="utf-8") == "hello"


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_delete_requires_approval_then_deletes_one_file 精确标识本用例的具体条件。
def test_delete_requires_approval_then_deletes_one_file(tmp_path: Path) -> None:
    target = tmp_path / "old.txt"
    target.write_text("remove me", encoding="utf-8")
    sandbox = WorkspaceSandbox(tmp_path)

    pending = delete_file(sandbox, "old.txt")
    assert pending.approval_required
    assert target.exists()

    result = delete_file(sandbox, "old.txt", approved=True)
    assert result.ok and result.changed
    assert result.metadata == {"path": "old.txt", "kind": "file"}
    assert not target.exists()


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_delete_is_idempotent_and_rejects_directories_or_escaped_paths 精确标识本用例的具体条件。
def test_delete_is_idempotent_and_rejects_directories_or_escaped_paths(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    (tmp_path / "folder").mkdir()

    missing = delete_file(sandbox, "missing.txt", approved=True)
    assert missing.ok and not missing.changed
    assert missing.metadata["path"] == "missing.txt"

    directory = delete_file(sandbox, "folder", approved=True)
    assert not directory.ok
    assert directory.error_code == "directory_not_allowed"
    assert (tmp_path / "folder").is_dir()

    escaped = delete_file(sandbox, "../outside.txt", approved=True)
    assert not escaped.ok
    assert escaped.error_code == "path_error"


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_delete_rejects_link_components_without_touching_the_target 精确标识本用例的具体条件。
def test_delete_rejects_link_components_without_touching_the_target(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError:
        return

    result = delete_file(WorkspaceSandbox(tmp_path), "linked.txt", approved=True)
    assert not result.ok
    assert result.error_code == "path_link_not_allowed"
    assert link.exists()
    assert target.read_text(encoding="utf-8") == "keep"


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_run_command_requires_approval_and_blocks_chaining 精确标识本用例的具体条件。
def test_run_command_requires_approval_and_blocks_chaining(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    pending = run_command(sandbox, ["python", "-c", "print('ok')"])
    assert pending.approval_required
    assert "当前用户权限" in pending.content

    blocked = run_command(sandbox, "python -c print(1) && python -c print(2)", approved=True)
    assert not blocked.ok
    assert blocked.error_code == "command_error"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_run_command_enforces_allowlist 精确标识本用例的具体条件。
def test_run_command_enforces_allowlist(tmp_path: Path) -> None:
    result = run_command(WorkspaceSandbox(tmp_path), ["definitely-not-allowed", "arg"], approved=True)
    assert not result.ok
    assert result.error_code == "command_not_allowed"


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_run_command_blocks_control_tokens_in_windows_batch_arguments 精确标识本用例的具体条件。
def test_run_command_blocks_control_tokens_in_windows_batch_arguments(tmp_path: Path) -> None:
    result = run_command(
        WorkspaceSandbox(tmp_path),
        ["npm.cmd", "--version", "&", "echo", "unexpected"],
        approved=True,
    )
    assert not result.ok
    assert result.error_code == "command_not_allowed"
    assert "批处理命令参数" in result.content


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_run_command_captures_output_when_approved 精确标识本用例的具体条件。
def test_run_command_captures_output_when_approved(tmp_path: Path) -> None:
    result = run_command(
        WorkspaceSandbox(tmp_path),
        ["python", "-c", "print('PGAGENT_OK')"],
        approved=True,
    )
    assert result.ok
    assert "PGAGENT_OK" in result.content
    assert result.metadata["security_scope"] == "current_user_host_permissions"


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_run_command_timeout_terminates_process_tree 精确标识本用例的具体条件。
def test_run_command_timeout_terminates_process_tree(tmp_path: Path) -> None:
    result = run_command(
        WorkspaceSandbox(tmp_path),
        ["python", "-c", "import time\ntime.sleep(2)"],
        approved=True,
        timeout_seconds=0.1,
    )
    assert not result.ok
    assert result.error_code == "timeout"
    assert result.metadata["process_tree_terminated"] is True


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_read_only_developer_tools_stay_inside_workspace 精确标识本用例的具体条件。
def test_read_only_developer_tools_stay_inside_workspace(tmp_path: Path) -> None:
    initialized = subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, capture_output=True, check=False
    )
    if initialized.returncode != 0:
        return
    tracked = tmp_path / "notes.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "notes.txt"], cwd=tmp_path, capture_output=True, check=False)
    subprocess.run(
        ["git", "-c", "user.name=PGAgent", "-c", "user.email=pgagent@example.invalid", "commit", "-qm", "seed"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    tracked.write_text("after\n", encoding="utf-8")

    sandbox = WorkspaceSandbox(tmp_path)
    status = git_status(sandbox)
    assert status.ok
    assert "notes.txt" in status.content
    diff = git_diff(sandbox, path="notes.txt")
    assert diff.ok
    assert "-before" in diff.content and "+after" in diff.content
    metadata = file_info(sandbox, "notes.txt")
    assert metadata.ok
    assert metadata.metadata["kind"] == "file"
    assert metadata.metadata["read_only"] is True


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_read_only_git_tools_report_non_repository 精确标识本用例的具体条件。
def test_read_only_git_tools_report_non_repository(tmp_path: Path) -> None:
    result = git_status(WorkspaceSandbox(tmp_path))
    assert not result.ok
    assert result.error_code == "not_git_repository"


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_timeout_kills_delayed_child_before_it_can_write 精确标识本用例的具体条件。
def test_timeout_kills_delayed_child_before_it_can_write(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived.txt"
    child_code = (
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(0.7)\n"
        "Path('child-survived.txt').write_text('bad')\n"
    )
    parent_code = (
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(5)\n"
    )
    result = run_command(
        WorkspaceSandbox(tmp_path),
        ["python", "-c", parent_code],
        approved=True,
        timeout_seconds=0.15,
    )
    assert result.error_code == "timeout"
    assert result.metadata["process_tree_terminated"] is True
    time.sleep(0.9)
    assert not marker.exists()


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_run_command_streams_but_retains_only_output_limit 精确标识本用例的具体条件。
def test_run_command_streams_but_retains_only_output_limit(tmp_path: Path) -> None:
    result = run_command(
        WorkspaceSandbox(tmp_path),
        ["python", "-c", "print('x' * 5000)"],
        approved=True,
        output_limit=256,
    )
    assert result.ok
    assert len(result.content) == 256
    assert result.metadata["truncated"] is True
