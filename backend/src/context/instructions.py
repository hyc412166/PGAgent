"""Codex-compatible global and project ``AGENTS.md`` instruction discovery."""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from src.config import settings


AGENTS_FILENAME = "AGENTS.md"
AGENTS_OVERRIDE_FILENAME = "AGENTS.override.md"
MAX_PERSONAL_INSTRUCTION_CHARS = 32_768
MAX_PROJECT_INSTRUCTION_BYTES = 32_768
_MAX_PERSONAL_INSTRUCTION_BYTES = MAX_PERSONAL_INSTRUCTION_CHARS * 4

_write_lock = threading.Lock()


@dataclass(frozen=True)
class InstructionSource:
    scope: str
    path: str
    content: str


def personal_agents_path() -> Path:
    return settings.data_dir / AGENTS_FILENAME


def personal_override_path() -> Path:
    return settings.data_dir / AGENTS_OVERRIDE_FILENAME


def _resolved_file_within(path: Path, boundary: Path) -> Path | None:
    try:
        resolved_boundary = boundary.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_boundary)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _read_bounded(
    path: Path,
    *,
    boundary: Path,
    max_bytes: int,
    max_characters: int | None = None,
) -> tuple[Path | None, str]:
    resolved = _resolved_file_within(path, boundary)
    if resolved is None:
        return None, ""
    with resolved.open("rb") as source:
        raw = source.read(max_bytes + 1)[:max_bytes]
    content = raw.decode("utf-8", errors="replace")
    if max_characters is not None:
        content = content[:max_characters]
    return resolved, content.strip()


def _read_personal_file(path: Path) -> tuple[Path | None, str]:
    data_dir = settings.data_dir
    if not data_dir.is_dir():
        return None, ""
    return _read_bounded(
        path,
        boundary=data_dir,
        max_bytes=_MAX_PERSONAL_INSTRUCTION_BYTES,
        max_characters=MAX_PERSONAL_INSTRUCTION_CHARS,
    )


def read_personal_instructions() -> str:
    _resolved, content = _read_personal_file(personal_agents_path())
    return content


def effective_personal_instructions() -> tuple[str, Path, bool]:
    override = personal_override_path()
    resolved_override, override_content = _read_personal_file(override)
    if resolved_override is not None and override_content:
        return override_content, resolved_override, True
    path = personal_agents_path()
    resolved_path, content = _read_personal_file(path)
    return content, resolved_path or path, False


def _read_project_file(path: Path, root: Path) -> tuple[Path | None, str]:
    if not root.is_dir():
        return None, ""
    return _read_bounded(
        path,
        boundary=root,
        max_bytes=MAX_PROJECT_INSTRUCTION_BYTES,
    )


def _selected_agents_file(directory: Path, *, scope: str) -> tuple[Path, str] | None:
    reader = _read_personal_file if scope == "global" else lambda path: _read_project_file(path, directory)
    for name in (AGENTS_OVERRIDE_FILENAME, AGENTS_FILENAME):
        resolved, content = reader(directory / name)
        if resolved is not None and content:
            return resolved, content
    return None


def write_personal_instructions(content: str) -> Path:
    normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) > MAX_PERSONAL_INSTRUCTION_CHARS:
        raise ValueError(f"custom instructions exceed {MAX_PERSONAL_INSTRUCTION_CHARS} characters")
    path = personal_agents_path()
    path.parent.mkdir(parents=True, exist_ok=True)
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
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
    return path


def discover_instruction_sources(workspace_root: str | Path) -> list[InstructionSource]:
    """Return global guidance followed by the selected workspace-root file."""

    sources: list[InstructionSource] = []
    global_source = _selected_agents_file(settings.data_dir, scope="global")
    if global_source is not None:
        global_path, global_content = global_source
        sources.append(InstructionSource("global", str(global_path), global_content))

    root = Path(workspace_root).resolve()
    project_source = _selected_agents_file(root, scope="project") if root.is_dir() else None
    if project_source is not None:
        project_path, project_content = project_source
        sources.append(InstructionSource(
            "project",
            str(project_path),
            project_content,
        ))
    return sources


def render_instruction_chain(sources: list[InstructionSource]) -> str:
    sections: list[str] = []
    for source in sources:
        title = "Personal instructions" if source.scope == "global" else "Project instructions"
        sections.append(f"### {title}\nSource: {source.path}\n\n{source.content}")
    return "\n\n".join(sections)


def load_instruction_chain(workspace_root: str | Path) -> tuple[str, list[str]]:
    sources = discover_instruction_sources(workspace_root)
    return render_instruction_chain(sources), [source.path for source in sources]


def render_workspace_rules(workspace_root: str, instructions: str) -> str:
    sections = [f"Only access the selected workspace: {workspace_root}"]
    if str(instructions or "").strip():
        sections.insert(0, str(instructions).strip())
    return "\n\n".join(sections)
