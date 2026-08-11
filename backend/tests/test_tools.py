from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from app.tools.builtins import list_files, read_file, run_command, search_files, write_file
from app.tools.sandbox import SandboxViolation, WorkspaceSandbox


def test_sandbox_rejects_parent_and_absolute_paths(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)

    for unsafe in ("../outside.txt", str((tmp_path.parent / "outside.txt").resolve())):
        try:
            sandbox.resolve(unsafe)
        except SandboxViolation:
            pass
        else:
            raise AssertionError(f"unsafe path was accepted: {unsafe}")


def test_read_file_cannot_escape_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "pgagent-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    result = read_file(WorkspaceSandbox(tmp_path), "../pgagent-outside.txt")
    assert not result.ok
    assert result.error_code == "path_error"
    assert "secret" not in result.content


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


def test_read_file_streams_only_requested_character_limit(tmp_path: Path) -> None:
    large = tmp_path / "large.txt"
    large.write_text("z" * 2_000_000, encoding="utf-8")
    result = read_file(WorkspaceSandbox(tmp_path), "large.txt", max_chars=100)
    assert result.ok
    assert len(result.content) == 100
    assert result.metadata["truncated"] is True
    assert result.metadata["size_bytes"] == 2_000_000


def test_read_file_does_not_mark_complete_crlf_text_truncated(tmp_path: Path) -> None:
    (tmp_path / "windows.txt").write_bytes(b"first\r\nsecond\r\n")
    result = read_file(WorkspaceSandbox(tmp_path), "windows.txt", max_chars=100)
    assert result.ok
    assert result.metadata["truncated"] is False


def test_search_reports_when_scan_limit_was_reached(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("needle" if name == "c.txt" else "other", encoding="utf-8")
    result = search_files(WorkspaceSandbox(tmp_path), "needle", max_entries=2)
    assert result.ok
    assert result.metadata["truncated"] is True
    assert result.metadata["visited"] == 2


def test_write_requires_approval_then_writes(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    pending = write_file(sandbox, "notes/result.txt", "hello")
    assert pending.approval_required
    assert pending.approval_request is not None
    assert not (tmp_path / "notes" / "result.txt").exists()

    result = write_file(sandbox, "notes/result.txt", "hello", approved=True)
    assert result.ok and result.changed
    assert (tmp_path / "notes" / "result.txt").read_text(encoding="utf-8") == "hello"


def test_run_command_requires_approval_and_blocks_chaining(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    pending = run_command(sandbox, ["python", "-c", "print('ok')"])
    assert pending.approval_required
    assert "当前用户权限" in pending.content

    blocked = run_command(sandbox, "python -c print(1) && python -c print(2)", approved=True)
    assert not blocked.ok
    assert blocked.error_code == "command_error"


def test_run_command_enforces_allowlist(tmp_path: Path) -> None:
    result = run_command(WorkspaceSandbox(tmp_path), ["definitely-not-allowed", "arg"], approved=True)
    assert not result.ok
    assert result.error_code == "command_not_allowed"


def test_run_command_captures_output_when_approved(tmp_path: Path) -> None:
    result = run_command(
        WorkspaceSandbox(tmp_path),
        ["python", "-c", "print('PGAGENT_OK')"],
        approved=True,
    )
    assert result.ok
    assert "PGAGENT_OK" in result.content
    assert result.metadata["security_scope"] == "current_user_host_permissions"


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
