"""Codex-compatible global and project ``AGENTS.md`` instruction discovery."""
# 文件职责：负责模型上下文组装、窗口预算与压缩中的 instructions 子模块。
# 逻辑关系：上层通过 context/instructions.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from src.config import settings


# 变量说明：AGENTS_FILENAME 表示当前步骤使用的 AGENTS_FILENAME 值。
AGENTS_FILENAME = "AGENTS.md"
# 变量说明：AGENTS_OVERRIDE_FILENAME 表示当前步骤使用的 AGENTS_OVERRIDE_FILENAME 值。
AGENTS_OVERRIDE_FILENAME = "AGENTS.override.md"
# 变量说明：MAX_PERSONAL_INSTRUCTION_CHARS 表示当前流程使用的 MAX_PERSONAL_INSTRUCTION_CHARS 集合。
MAX_PERSONAL_INSTRUCTION_CHARS = 32_768
# 变量说明：MAX_PROJECT_INSTRUCTION_BYTES 表示当前流程使用的 MAX_PROJECT_INSTRUCTION_BYTES 集合。
MAX_PROJECT_INSTRUCTION_BYTES = 32_768
# 变量说明：_MAX_PERSONAL_INSTRUCTION_BYTES 表示当前流程使用的 _MAX_PERSONAL_INSTRUCTION_BYTES 集合。
_MAX_PERSONAL_INSTRUCTION_BYTES = MAX_PERSONAL_INSTRUCTION_CHARS * 4

# 变量说明：_write_lock 表示当前步骤使用的 _write_lock 值。
_write_lock = threading.Lock()


# 类职责：定义 InstructionSource 在本领域中的数据与行为。
@dataclass(frozen=True)
class InstructionSource:
    # 变量说明：scope 表示当前步骤使用的 scope 值。
    scope: str
    # 变量说明：path 表示当前文件或目录路径。
    path: str
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str


# 函数职责：完成 personal_agents_path 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def personal_agents_path() -> Path:
    return settings.data_dir / AGENTS_FILENAME


# 函数职责：完成 personal_override_path 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def personal_override_path() -> Path:
    return settings.data_dir / AGENTS_OVERRIDE_FILENAME


# 函数职责：完成 resolved_file_within 对应的业务处理。
# 参数关系：path 表示当前文件或目录路径；boundary 表示当前步骤使用的 boundary 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _resolved_file_within(path: Path, boundary: Path) -> Path | None:
    try:
        # 变量说明：resolved_boundary 表示当前步骤使用的 resolved_boundary 值。
        resolved_boundary = boundary.resolve(strict=True)
        # 变量说明：resolved 表示当前步骤使用的 resolved 值。
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_boundary)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_file() else None


# 函数职责：完成 read_bounded 对应的业务处理。
# 参数关系：path 表示当前文件或目录路径；boundary 表示当前步骤使用的 boundary 值；max_bytes 表示当前流程使用的 max_bytes 集合；max_characters 表示当前流程使用的 max_characters 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _read_bounded(
    path: Path,
    *,
    boundary: Path,
    max_bytes: int,
    max_characters: int | None = None,
) -> tuple[Path | None, str]:
    # 变量说明：resolved 表示当前步骤使用的 resolved 值。
    resolved = _resolved_file_within(path, boundary)
    if resolved is None:
        return None, ""
    with resolved.open("rb") as source:
        # 变量说明：raw 表示当前步骤使用的 raw 值。
        raw = source.read(max_bytes + 1)[:max_bytes]
    # 变量说明：content 表示待处理或返回的正文内容。
    content = raw.decode("utf-8", errors="replace")
    if max_characters is not None:
        # 变量说明：content 表示待处理或返回的正文内容。
        content = content[:max_characters]
    return resolved, content.strip()


# 函数职责：完成 read_personal_file 对应的业务处理。
# 参数关系：path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _read_personal_file(path: Path) -> tuple[Path | None, str]:
    # 变量说明：data_dir 表示data_dir 对应的文件系统位置。
    data_dir = settings.data_dir
    if not data_dir.is_dir():
        return None, ""
    return _read_bounded(
        path,
        boundary=data_dir,
        max_bytes=_MAX_PERSONAL_INSTRUCTION_BYTES,
        max_characters=MAX_PERSONAL_INSTRUCTION_CHARS,
    )


# 函数职责：完成 read_personal_instructions 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def read_personal_instructions() -> str:
    # 变量说明：_resolved 表示当前步骤使用的 _resolved 值；content 表示待处理或返回的正文内容。
    _resolved, content = _read_personal_file(personal_agents_path())
    return content


# 函数职责：完成 effective_personal_instructions 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def effective_personal_instructions() -> tuple[str, Path, bool]:
    # 变量说明：override 表示当前步骤使用的 override 值。
    override = personal_override_path()
    # 变量说明：resolved_override 表示当前步骤使用的 resolved_override 值；override_content 表示当前步骤使用的 override_content 值。
    resolved_override, override_content = _read_personal_file(override)
    if resolved_override is not None and override_content:
        return override_content, resolved_override, True
    # 变量说明：path 表示当前文件或目录路径。
    path = personal_agents_path()
    # 变量说明：resolved_path 表示resolved_path 对应的文件系统位置；content 表示待处理或返回的正文内容。
    resolved_path, content = _read_personal_file(path)
    return content, resolved_path or path, False


# 函数职责：完成 read_project_file 对应的业务处理。
# 参数关系：path 表示当前文件或目录路径；root 表示处理范围的根目录。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _read_project_file(path: Path, root: Path) -> tuple[Path | None, str]:
    if not root.is_dir():
        return None, ""
    return _read_bounded(
        path,
        boundary=root,
        max_bytes=MAX_PROJECT_INSTRUCTION_BYTES,
    )


# 函数职责：完成 selected_agents_file 对应的业务处理。
# 参数关系：directory 表示当前步骤使用的 directory 值；scope 表示当前步骤使用的 scope 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _selected_agents_file(directory: Path, *, scope: str) -> tuple[Path, str] | None:
    # 变量说明：reader 表示当前步骤使用的 reader 值。
    reader = _read_personal_file if scope == "global" else lambda path: _read_project_file(path, directory)
    for name in (AGENTS_OVERRIDE_FILENAME, AGENTS_FILENAME):
        # 变量说明：resolved 表示当前步骤使用的 resolved 值；content 表示待处理或返回的正文内容。
        resolved, content = reader(directory / name)
        if resolved is not None and content:
            return resolved, content
    return None


# 函数职责：完成 write_personal_instructions 对应的业务处理。
# 参数关系：content 表示待处理或返回的正文内容。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def write_personal_instructions(content: str) -> Path:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) > MAX_PERSONAL_INSTRUCTION_CHARS:
        raise ValueError(f"custom instructions exceed {MAX_PERSONAL_INSTRUCTION_CHARS} characters")
    # 变量说明：path 表示当前文件或目录路径。
    path = personal_agents_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 变量说明：temporary_name 表示当前步骤使用的 temporary_name 值。
    temporary_name = ""
    with _write_lock:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(normalized)
                # 变量说明：temporary_name 表示当前步骤使用的 temporary_name 值。
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
    return path


# 函数职责：完成 discover_instruction_sources 对应的业务处理。
# 参数关系：workspace_root 表示当前步骤使用的 workspace_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def discover_instruction_sources(workspace_root: str | Path) -> list[InstructionSource]:
    """Return global guidance followed by the selected workspace-root file."""

    # 变量说明：sources 表示当前流程使用的 sources 集合。
    sources: list[InstructionSource] = []
    # 变量说明：global_source 表示当前步骤使用的 global_source 值。
    global_source = _selected_agents_file(settings.data_dir, scope="global")
    if global_source is not None:
        # 变量说明：global_path 表示global_path 对应的文件系统位置；global_content 表示当前步骤使用的 global_content 值。
        global_path, global_content = global_source
        sources.append(InstructionSource("global", str(global_path), global_content))

    # 变量说明：root 表示处理范围的根目录。
    root = Path(workspace_root).resolve()
    # 变量说明：project_source 表示当前步骤使用的 project_source 值。
    project_source = _selected_agents_file(root, scope="project") if root.is_dir() else None
    if project_source is not None:
        # 变量说明：project_path 表示project_path 对应的文件系统位置；project_content 表示当前步骤使用的 project_content 值。
        project_path, project_content = project_source
        sources.append(InstructionSource(
            "project",
            str(project_path),
            project_content,
        ))
    return sources


# 函数职责：完成 render_instruction_chain 对应的业务处理。
# 参数关系：sources 表示当前流程使用的 sources 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def render_instruction_chain(sources: list[InstructionSource]) -> str:
    # 变量说明：sections 表示当前流程使用的 sections 集合。
    sections: list[str] = []
    for source in sources:
        # 变量说明：title 表示当前步骤使用的 title 值。
        title = "Personal instructions" if source.scope == "global" else "Project instructions"
        sections.append(f"### {title}\nSource: {source.path}\n\n{source.content}")
    return "\n\n".join(sections)


# 函数职责：加载 instruction_chain 对应的数据或流程。
# 参数关系：workspace_root 表示当前步骤使用的 workspace_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def load_instruction_chain(workspace_root: str | Path) -> tuple[str, list[str]]:
    # 变量说明：sources 表示当前流程使用的 sources 集合。
    sources = discover_instruction_sources(workspace_root)
    return render_instruction_chain(sources), [source.path for source in sources]


# 函数职责：完成 render_workspace_rules 对应的业务处理。
# 参数关系：workspace_root 表示当前步骤使用的 workspace_root 值；instructions 表示当前流程使用的 instructions 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def render_workspace_rules(workspace_root: str, instructions: str) -> str:
    # 变量说明：sections 表示当前流程使用的 sections 集合。
    sections = [f"Only access the selected workspace: {workspace_root}"]
    if str(instructions or "").strip():
        sections.insert(0, str(instructions).strip())
    return "\n\n".join(sections)
