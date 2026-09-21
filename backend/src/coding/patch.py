"""Parse and apply bounded, workspace-scoped multi-file text patches."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 patch 子模块。
# 逻辑关系：上层通过 coding/patch.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from src.tools.sandbox import SandboxViolation, WorkspaceSandbox
from src.tools.types import ApprovalRequest, ToolResult

from .changes import build_file_change


# 类职责：表示 PatchError 场景的领域异常。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class PatchError(ValueError):
    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：message 表示当前消息；code 表示当前步骤使用的 code 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self, message: str, code: str = "invalid_patch") -> None:
        super().__init__(message)
        # 变量说明：code 表示当前步骤使用的 code 值。
        self.code = code


# 类职责：定义 PatchHunk 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class PatchHunk:
    # 变量说明：lines 表示当前流程使用的 lines 集合。
    lines: tuple[str, ...]


# 类职责：定义 FilePatch 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class FilePatch:
    # 变量说明：operation 表示当前步骤使用的 operation 值。
    operation: str
    # 变量说明：path 表示当前文件或目录路径。
    path: str
    # 变量说明：hunks 表示当前流程使用的 hunks 集合。
    hunks: tuple[PatchHunk, ...] = ()
    # 变量说明：added_lines 表示当前流程使用的 added_lines 集合。
    added_lines: tuple[str, ...] = ()


# 类职责：定义 PreparedChange 在本领域中的数据与行为。
@dataclass(slots=True)
class PreparedChange:
    # 变量说明：operation 表示当前步骤使用的 operation 值。
    operation: str
    # 变量说明：path 表示当前文件或目录路径。
    path: str
    # 变量说明：target 表示当前步骤使用的 target 值。
    target: Path
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str | None
    # 变量说明：hunks 表示当前流程使用的 hunks 集合。
    hunks: int
    # 变量说明：added_lines 表示当前流程使用的 added_lines 集合。
    added_lines: int
    # 变量说明：deleted_lines 表示当前流程使用的 deleted_lines 集合。
    deleted_lines: int
    # 变量说明：original_bytes 表示当前流程使用的 original_bytes 集合。
    original_bytes: bytes | None
    # 变量说明：original_mode 表示当前步骤使用的 original_mode 值。
    original_mode: int | None


# 变量说明：_FILE_HEADERS 表示当前流程使用的 _FILE_HEADERS 集合。
_FILE_HEADERS = {
    "*** Add File: ": "add",
    "*** Update File: ": "update",
    "*** Delete File: ": "delete",
}


# 函数职责：解析 patch 对应的数据或流程。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def parse_patch(value: str) -> list[FilePatch]:
    # 变量说明：text 表示当前步骤使用的 text 值。
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    # 变量说明：lines 表示当前流程使用的 lines 集合。
    lines = text.split("\n")
    if not lines or lines[0] != "*** Begin Patch":
        raise PatchError("patch must start with *** Begin Patch")
    if lines[-1] == "":
        lines.pop()
    if not lines or lines[-1] != "*** End Patch":
        raise PatchError("patch must end with *** End Patch")

    # 变量说明：file_patches 表示当前流程使用的 file_patches 集合。
    file_patches: list[FilePatch] = []
    # 变量说明：seen_paths 表示当前流程使用的 seen_paths 集合。
    seen_paths: set[str] = set()
    # 变量说明：index 表示当前元素的位置索引。
    index = 1
    while index < len(lines) - 1:
        # 变量说明：header 表示当前步骤使用的 header 值。
        header = lines[index]
        # 变量说明：operation 表示当前步骤使用的 operation 值。
        operation = ""
        # 变量说明：path 表示当前文件或目录路径。
        path = ""
        for prefix, candidate in _FILE_HEADERS.items():
            if header.startswith(prefix):
                # 变量说明：operation 表示当前步骤使用的 operation 值。
                operation = candidate
                # 变量说明：path 表示当前文件或目录路径。
                path = header[len(prefix):].strip()
                break
        if not operation or not path:
            raise PatchError(f"expected file header at patch line {index + 1}")
        if path in seen_paths:
            raise PatchError(f"path appears more than once in patch: {path}")
        seen_paths.add(path)
        index += 1

        # 变量说明：body 表示当前步骤使用的 body 值。
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

        # 变量说明：hunks 表示当前流程使用的 hunks 集合。
        hunks: list[PatchHunk] = []
        # 变量说明：body_index 表示当前步骤使用的 body_index 值。
        body_index = 0
        while body_index < len(body):
            if not body[body_index].startswith("@@"):
                raise PatchError(f"update section must start each hunk with @@: {path}")
            body_index += 1
            # 变量说明：hunk_lines 表示当前流程使用的 hunk_lines 集合。
            hunk_lines: list[str] = []
            while body_index < len(body) and not body[body_index].startswith("@@"):
                # 变量说明：line 表示当前步骤使用的 line 值。
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


# 函数职责：完成 decode_text 对应的业务处理。
# 参数关系：target 表示当前步骤使用的 target 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _decode_text(target: Path) -> tuple[bytes, str]:
    # 变量说明：data 表示当前处理的数据。
    data = target.read_bytes()
    if b"\x00" in data[:8192]:
        raise PatchError(f"binary files are not supported: {target.name}", "binary_file")
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"file is not valid UTF-8: {target.name}", "invalid_encoding") from exc


# 函数职责：完成 split_file_text 对应的业务处理。
# 参数关系：text 表示当前步骤使用的 text 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _split_file_text(text: str) -> tuple[list[str], str, bool]:
    # 变量说明：newline 表示当前步骤使用的 newline 值。
    newline = "\r\n" if "\r\n" in text else "\n"
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # 变量说明：trailing_newline 表示当前步骤使用的 trailing_newline 值。
    trailing_newline = normalized.endswith("\n")
    # 变量说明：lines 表示当前流程使用的 lines 集合。
    lines = normalized.split("\n")
    if trailing_newline:
        lines.pop()
    return lines, newline, trailing_newline


# 函数职责：完成 join_file_text 对应的业务处理。
# 参数关系：lines 表示当前流程使用的 lines 集合；newline 表示当前步骤使用的 newline 值；trailing_newline 表示当前步骤使用的 trailing_newline 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _join_file_text(lines: list[str], newline: str, trailing_newline: bool) -> str:
    # 变量说明：text 表示当前步骤使用的 text 值。
    text = newline.join(lines)
    return text + newline if trailing_newline else text


# 函数职责：应用 hunks 对应的数据或流程。
# 参数关系：path 表示当前文件或目录路径；original 表示当前步骤使用的 original 值；hunks 表示当前流程使用的 hunks 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _apply_hunks(path: str, original: str, hunks: tuple[PatchHunk, ...]) -> tuple[str, int, int]:
    # 变量说明：lines 表示当前流程使用的 lines 集合；newline 表示当前步骤使用的 newline 值；trailing_newline 表示当前步骤使用的 trailing_newline 值。
    lines, newline, trailing_newline = _split_file_text(original)
    # 变量说明：added 表示当前步骤使用的 added 值。
    added = 0
    # 变量说明：deleted 表示当前步骤使用的 deleted 值。
    deleted = 0
    for hunk in hunks:
        # 变量说明：old_lines 表示当前流程使用的 old_lines 集合。
        old_lines = [line[1:] for line in hunk.lines if line[0] in {" ", "-"}]
        # 变量说明：new_lines 表示当前流程使用的 new_lines 集合。
        new_lines = [line[1:] for line in hunk.lines if line[0] in {" ", "+"}]
        if not old_lines:
            raise PatchError(f"insertion hunk requires context in {path}", "patch_context_missing")
        # 变量说明：matches 表示当前流程使用的 matches 集合。
        matches = [
            index
            for index in range(0, len(lines) - len(old_lines) + 1)
            if lines[index:index + len(old_lines)] == old_lines
        ]
        if not matches:
            raise PatchError(f"hunk context was not found in {path}", "patch_context_not_found")
        if len(matches) > 1:
            raise PatchError(f"hunk context is ambiguous in {path}", "patch_context_ambiguous")
        # 变量说明：start 表示当前步骤使用的 start 值。
        start = matches[0]
        lines[start:start + len(old_lines)] = new_lines
        added += sum(1 for line in hunk.lines if line.startswith("+"))
        deleted += sum(1 for line in hunk.lines if line.startswith("-"))
    return _join_file_text(lines, newline, trailing_newline), added, deleted


# 函数职责：准备 patch 对应的数据或流程。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；patches 表示当前流程使用的 patches 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def prepare_patch(sandbox: WorkspaceSandbox, patches: list[FilePatch]) -> list[PreparedChange]:
    # 变量说明：prepared 表示当前步骤使用的 prepared 值。
    prepared: list[PreparedChange] = []
    for item in patches:
        try:
            # 变量说明：target 表示当前步骤使用的 target 值。
            target = sandbox.resolve(item.path, must_exist=item.operation != "add")
        except (SandboxViolation, FileNotFoundError, OSError) as exc:
            raise PatchError(str(exc), "path_error") from exc
        # 变量说明：relative 表示当前步骤使用的 relative 值。
        relative = sandbox.relative(target)
        if item.operation == "add":
            if target.exists():
                raise PatchError(f"file already exists: {relative}", "file_exists")
            # 变量说明：content 表示待处理或返回的正文内容。
            content = "\n".join(item.added_lines)
            if item.added_lines:
                content += "\n"
            prepared.append(PreparedChange(
                "add", relative, target, content, 0, len(item.added_lines), 0, None, None,
            ))
            continue
        if not target.is_file():
            raise PatchError(f"target is not a file: {relative}", "not_file")
        # 变量说明：original_bytes 表示当前流程使用的 original_bytes 集合；original 表示当前步骤使用的 original 值。
        original_bytes, original = _decode_text(target)
        # 变量说明：original_mode 表示当前步骤使用的 original_mode 值。
        original_mode = target.stat().st_mode
        if item.operation == "delete":
            prepared.append(PreparedChange(
                "delete", relative, target, None, 0, 0,
                len(original.splitlines()), original_bytes, original_mode,
            ))
            continue
        # 变量说明：updated 表示当前步骤使用的 updated 值；added 表示当前步骤使用的 added 值；deleted 表示当前步骤使用的 deleted 值。
        updated, added, deleted = _apply_hunks(relative, original, item.hunks)
        prepared.append(PreparedChange(
            "update", relative, target, updated, len(item.hunks), added, deleted,
            original_bytes, original_mode,
        ))
    return prepared


# 函数职责：完成 stage_bytes 对应的业务处理。
# 参数关系：target 表示当前步骤使用的 target 值；content 表示待处理或返回的正文内容；mode 表示当前步骤使用的 mode 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _stage_bytes(target: Path, content: bytes, mode: int | None) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    # 变量说明：temporary 表示当前步骤使用的 temporary 值。
    temporary = NamedTemporaryFile(
        mode="wb",
        prefix=".pgagent-patch-",
        dir=target.parent,
        delete=False,
    )
    # 变量说明：staged_path 表示staged_path 对应的文件系统位置。
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


# 函数职责：完成 change_record 对应的业务处理。
# 参数关系：change 表示当前步骤使用的 change 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _change_record(change: PreparedChange) -> dict[str, object]:
    after = None if change.operation == "delete" else (change.content or "").encode("utf-8")
    record = build_file_change(change.path, change.operation, change.original_bytes, after)
    return {
        **record,
        "path": change.path,
        "operation": change.operation,
        "hunks": change.hunks,
        "added_lines": change.added_lines,
        "deleted_lines": change.deleted_lines,
    }


# 函数职责：完成 restore_change 对应的业务处理。
# 参数关系：change 表示当前步骤使用的 change 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _restore_change(change: PreparedChange) -> None:
    if change.original_bytes is None:
        change.target.unlink(missing_ok=True)
        return
    # 变量说明：staged_path 表示staged_path 对应的文件系统位置。
    staged_path = _stage_bytes(change.target, change.original_bytes, change.original_mode)
    try:
        os.replace(staged_path, change.target)
    finally:
        staged_path.unlink(missing_ok=True)


# 函数职责：应用 patch 对应的数据或流程。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；patch 表示当前步骤使用的 patch 值；approved 表示当前步骤使用的 approved 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def apply_patch(
    sandbox: WorkspaceSandbox,
    patch: str,
    *,
    approved: bool = False,
) -> ToolResult:
    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments = {"patch": patch}
    if not approved:
        # 变量说明：reason 表示当前步骤使用的 reason 值。
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
        # 变量说明：prepared 表示当前步骤使用的 prepared 值。
        prepared = prepare_patch(sandbox, parse_patch(patch))
        # 变量说明：staged 表示当前步骤使用的 staged 值。
        staged: dict[str, Path] = {}
        # 变量说明：applied 表示当前步骤使用的 applied 值。
        applied: list[PreparedChange] = []
        try:
            for change in prepared:
                if change.operation != "delete":
                    # 变量说明：staged 的索引项 表示该语句创建或更新的目标数据。
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
            # 变量说明：rollback_failed 表示当前步骤使用的 rollback_failed 值。
            rollback_failed: list[PreparedChange] = []
            for change in reversed(applied):
                try:
                    _restore_change(change)
                except OSError:
                    rollback_failed.append(change)
            # 变量说明：metadata 表示当前步骤使用的 metadata 值。
            metadata = {}
            if rollback_failed:
                # 变量说明：files 表示当前流程使用的 files 集合。
                files = [_change_record(change) for change in rollback_failed]
                metadata["change_set"] = {
                    "status": "partial_failure",
                    "file_count": len(files),
                    "files": files,
                }
            # 变量说明：detail 表示当前步骤使用的 detail 值。
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
        # 变量说明：files 表示当前流程使用的 files 集合。
        files = [_change_record(change) for change in prepared]
        # 变量说明：summary 表示当前步骤使用的 summary 值。
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
