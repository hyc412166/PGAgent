"""Filesystem boundary enforcement for a PGAgent workspace."""

from __future__ import annotations

from pathlib import Path


class SandboxViolation(ValueError):
    """Raised when a path attempts to escape its assigned workspace."""


class WorkspaceSandbox:
    """Resolve all file access beneath one immutable workspace root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str | Path = ".", *, must_exist: bool = False) -> Path:
        candidate_input = Path(relative_path)
        if candidate_input.is_absolute():
            raise SandboxViolation("只允许使用工作区内的相对路径")

        candidate = (self.root / candidate_input).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise SandboxViolation("路径越过了工作区边界") from exc

        if must_exist and not candidate.exists():
            raise FileNotFoundError(f"路径不存在: {relative_path}")
        return candidate

    def relative(self, path: Path) -> str:
        # Display the lexical path, not a symlink's target. Access checks always go
        # through ``resolve`` separately, so an outward symlink can be listed but
        # can never be followed by read/write tools.
        return path.absolute().relative_to(self.root).as_posix() or "."
