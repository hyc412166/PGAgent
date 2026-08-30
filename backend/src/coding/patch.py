"""Parse and apply bounded, workspace-scoped multi-file text patches."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from src.tools.sandbox import SandboxViolation, WorkspaceSandbox
from src.tools.types import ApprovalRequest, ToolResult


class PatchError(ValueError):
    def __init__(self, message: str, code: str = "invalid_patch") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PatchHunk:
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FilePatch:
    operation: str
    path: str
    hunks: tuple[PatchHunk, ...] = ()
    added_lines: tuple[str, ...] = ()


@dataclass(slots=True)
class PreparedChange:
    operation: str
    path: str
    target: Path
    content: str | None
    hunks: int
    added_lines: int
    deleted_lines: int
    original_bytes: bytes | None
    original_mode: int | None


_FILE_HEADERS = {
    "*** Add File: ": "add",
    "*** Update File: ": "update",
    "*** Delete File: ": "delete",
}


def parse_patch(value: str) -> list[FilePatch]:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0] != "*** Begin Patch":
        raise PatchError("patch must start with *** Begin Patch")
    if lines[-1] == "":
        lines.pop()
    if not lines or lines[-1] != "*** End Patch":
        raise PatchError("patch must end with *** End Patch")

    file_patches: list[FilePatch] = []
    seen_paths: set[str] = set()
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        operation = ""
        path = ""
        for prefix, candidate in _FILE_HEADERS.items():
            if header.startswith(prefix):
                operation = candidate
                path = header[len(prefix):].strip()
                break
        if not operation or not path:
            raise PatchError(f"expected file header at patch line {index + 1}")
        if path in seen_paths:
            raise PatchError(f"path appears more than once in patch: {path}")
        seen_paths.add(path)
        index += 1

        body: list[str] = []
        while index < len(lines) - 1 and not any(lines[index].startswith(prefix) for prefix in _FILE_HEADERS):
            body.append(lines[index])
            index += 1

        if operation == "add":
            if any(not line.startswith("+") for line in body):
                raise PatchError(f"added file lines must start with +: {path}")
            file_patches.append(FilePatch(operation, path, added_lines=tuple(line[1:] for line in body)))
            continue
        if operation == "delete":
            if body:
                raise PatchError(f"delete file section cannot contain hunks: {path}")
            file_patches.append(FilePatch(operation, path))
            continue

        hunks: list[PatchHunk] = []
        body_index = 0
        while body_index < len(body):
            if not body[body_index].startswith("@@"):
                raise PatchError(f"update section must start each hunk with @@: {path}")
            body_index += 1
            hunk_lines: list[str] = []
            while body_index < len(body) and not body[body_index].startswith("@@"):
                line = body[body_index]
                if not line or line[0] not in {" ", "+", "-"}:
                    raise PatchError(f"invalid hunk line for {path}: {line!r}")
                hunk_lines.append(line)
                body_index += 1
            if not hunk_lines or not any(line[0] in {"+", "-"} for line in hunk_lines):
                raise PatchError(f"hunk must contain an addition or deletion: {path}")
            hunks.append(PatchHunk(tuple(hunk_lines)))
        if not hunks:
            raise PatchError(f"update file section has no hunks: {path}")
        file_patches.append(FilePatch(operation, path, hunks=tuple(hunks)))
    return file_patches


def _decode_text(target: Path) -> tuple[bytes, str]:
    data = target.read_bytes()
    if b"\x00" in data[:8192]:
        raise PatchError(f"binary files are not supported: {target.name}", "binary_file")
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"file is not valid UTF-8: {target.name}", "invalid_encoding") from exc


def _split_file_text(text: str) -> tuple[list[str], str, bool]:
    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    trailing_newline = normalized.endswith("\n")
    lines = normalized.split("\n")
    if trailing_newline:
        lines.pop()
    return lines, newline, trailing_newline


def _join_file_text(lines: list[str], newline: str, trailing_newline: bool) -> str:
    text = newline.join(lines)
    return text + newline if trailing_newline else text


def _apply_hunks(path: str, original: str, hunks: tuple[PatchHunk, ...]) -> tuple[str, int, int]:
    lines, newline, trailing_newline = _split_file_text(original)
    added = 0
    deleted = 0
    for hunk in hunks:
        old_lines = [line[1:] for line in hunk.lines if line[0] in {" ", "-"}]
        new_lines = [line[1:] for line in hunk.lines if line[0] in {" ", "+"}]
        if not old_lines:
            raise PatchError(f"insertion hunk requires context in {path}", "patch_context_missing")
        matches = [
            index
            for index in range(0, len(lines) - len(old_lines) + 1)
            if lines[index:index + len(old_lines)] == old_lines
        ]
        if not matches:
            raise PatchError(f"hunk context was not found in {path}", "patch_context_not_found")
        if len(matches) > 1:
            raise PatchError(f"hunk context is ambiguous in {path}", "patch_context_ambiguous")
        start = matches[0]
        lines[start:start + len(old_lines)] = new_lines
        added += sum(1 for line in hunk.lines if line.startswith("+"))
        deleted += sum(1 for line in hunk.lines if line.startswith("-"))
    return _join_file_text(lines, newline, trailing_newline), added, deleted


def prepare_patch(sandbox: WorkspaceSandbox, patches: list[FilePatch]) -> list[PreparedChange]:
    prepared: list[PreparedChange] = []
    for item in patches:
        try:
            target = sandbox.resolve(item.path, must_exist=item.operation != "add")
        except (SandboxViolation, FileNotFoundError, OSError) as exc:
            raise PatchError(str(exc), "path_error") from exc
        relative = sandbox.relative(target)
        if item.operation == "add":
            if target.exists():
                raise PatchError(f"file already exists: {relative}", "file_exists")
            content = "\n".join(item.added_lines)
            if item.added_lines:
                content += "\n"
            prepared.append(PreparedChange(
                "add", relative, target, content, 0, len(item.added_lines), 0, None, None,
            ))
            continue
        if not target.is_file():
            raise PatchError(f"target is not a file: {relative}", "not_file")
        original_bytes, original = _decode_text(target)
        original_mode = target.stat().st_mode
        if item.operation == "delete":
            prepared.append(PreparedChange(
                "delete", relative, target, None, 0, 0,
                len(original.splitlines()), original_bytes, original_mode,
            ))
            continue
        updated, added, deleted = _apply_hunks(relative, original, item.hunks)
        prepared.append(PreparedChange(
            "update", relative, target, updated, len(item.hunks), added, deleted,
            original_bytes, original_mode,
        ))
    return prepared


def _stage_bytes(target: Path, content: bytes, mode: int | None) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = NamedTemporaryFile(
        mode="wb",
        prefix=".pgagent-patch-",
        dir=target.parent,
        delete=False,
    )
    staged_path = Path(temporary.name)
    try:
        with temporary:
            temporary.write(content)
        if mode is not None:
            os.chmod(staged_path, mode)
        return staged_path
    except OSError:
        staged_path.unlink(missing_ok=True)
        raise


def _change_record(change: PreparedChange) -> dict[str, object]:
    return {
        "path": change.path,
        "operation": change.operation,
        "hunks": change.hunks,
        "added_lines": change.added_lines,
        "deleted_lines": change.deleted_lines,
    }


def _restore_change(change: PreparedChange) -> None:
    if change.original_bytes is None:
        change.target.unlink(missing_ok=True)
        return
    staged_path = _stage_bytes(change.target, change.original_bytes, change.original_mode)
    try:
        os.replace(staged_path, change.target)
    finally:
        staged_path.unlink(missing_ok=True)


def apply_patch(
    sandbox: WorkspaceSandbox,
    patch: str,
    *,
    approved: bool = False,
) -> ToolResult:
    arguments = {"patch": patch}
    if not approved:
        reason = "应用代码补丁需要批准"
        return ToolResult(
            "apply_patch",
            False,
            reason,
            approval_required=True,
            approval_request=ApprovalRequest("apply_patch", arguments, reason),
            error_code="approval_required",
        )
    try:
        prepared = prepare_patch(sandbox, parse_patch(patch))
        staged: dict[str, Path] = {}
        applied: list[PreparedChange] = []
        try:
            for change in prepared:
                if change.operation != "delete":
                    staged[change.path] = _stage_bytes(
                        change.target,
                        (change.content or "").encode("utf-8"),
                        change.original_mode,
                    )
            for change in prepared:
                if change.operation == "delete":
                    change.target.unlink()
                else:
                    os.replace(staged[change.path], change.target)
                    staged.pop(change.path)
                applied.append(change)
        except OSError as exc:
            rollback_failed: list[PreparedChange] = []
            for change in reversed(applied):
                try:
                    _restore_change(change)
                except OSError:
                    rollback_failed.append(change)
            metadata = {}
            if rollback_failed:
                files = [_change_record(change) for change in rollback_failed]
                metadata["change_set"] = {
                    "status": "partial_failure",
                    "file_count": len(files),
                    "files": files,
                }
            detail = str(exc)
            if rollback_failed:
                detail += "; rollback failed for: " + ", ".join(change.path for change in rollback_failed)
            return ToolResult(
                "apply_patch",
                False,
                detail,
                changed=bool(rollback_failed),
                error_code="patch_write_error",
                metadata=metadata,
            )
        finally:
            for staged_path in staged.values():
                staged_path.unlink(missing_ok=True)
        files = [_change_record(change) for change in prepared]
        summary = "\n".join(
            f"- {item['operation']} {item['path']} (+{item['added_lines']} -{item['deleted_lines']})"
            for item in files
        )
        return ToolResult(
            "apply_patch",
            True,
            f"Applied patch to {len(files)} file(s).\n{summary}",
            changed=bool(files),
            metadata={"change_set": {"status": "applied", "file_count": len(files), "files": files}},
        )
    except PatchError as exc:
        return ToolResult("apply_patch", False, str(exc), error_code=exc.code)
    except OSError as exc:
        return ToolResult("apply_patch", False, str(exc), error_code="patch_write_error")
