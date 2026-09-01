"""Explicit execution runtimes for project validation commands."""

from __future__ import annotations

import threading
from pathlib import PurePosixPath
from typing import Any, Mapping
import uuid

from src.tools import builtins
from src.tools.sandbox import WorkspaceSandbox
from src.tools.types import ToolResult


_DOCKER_COMMAND_ALLOWLIST = frozenset({"docker", "docker.exe"})


def normalize_validation_runtime(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    kind = str(raw.get("kind") or "local").strip().lower()
    if kind == "local":
        return {"kind": "local"}
    if kind != "docker":
        raise ValueError(f"unsupported validation runtime: {kind}")
    image = str(raw.get("image") or "").strip()
    if not image:
        raise ValueError("Docker validation runtime requires an image")
    if image.startswith("-"):
        raise ValueError("Docker validation image must not start with '-'")
    if len(image) > 500 or any(character.isspace() or ord(character) < 32 for character in image):
        raise ValueError("invalid Docker validation image")
    return {"kind": "docker", "image": image}


def run_in_validation_runtime(
    sandbox: WorkspaceSandbox,
    command: str | list[str],
    *,
    runtime: Mapping[str, Any] | None,
    cwd: str,
    timeout_seconds: float,
    approved: bool,
    cancel_event: threading.Event | None,
) -> ToolResult:
    config = normalize_validation_runtime(runtime)
    if config["kind"] == "local":
        return builtins.run_command(
            sandbox,
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            approved=approved,
            _cancel_event=cancel_event,
        )

    if not approved:
        return builtins.run_command(
            sandbox,
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            approved=False,
            _cancel_event=cancel_event,
        )

    try:
        working_directory = sandbox.resolve(cwd, must_exist=True)
        if not working_directory.is_dir():
            return ToolResult("validate", False, "cwd is not a directory", error_code="not_directory")
        relative_cwd = sandbox.relative(working_directory)
        inner_argv = builtins.parse_command_argv(command)
    except (ValueError, OSError) as exc:
        return ToolResult("validate", False, str(exc), error_code="command_error")

    # SWE-bench images install the checked-out project at /testbed. Mounting
    # the candidate there preserves their dependency and editable-import paths.
    container_root = PurePosixPath("/testbed")
    container_cwd = container_root.joinpath(*PurePosixPath(relative_cwd).parts).as_posix()
    container_name = f"pgagent-validation-{uuid.uuid4().hex}"
    docker_argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--pull",
        "never",
        "--network",
        "none",
        "--volume",
        f"{sandbox.root}:/testbed",
        "--workdir",
        container_cwd,
        str(config["image"]),
        *inner_argv,
    ]
    result = builtins.run_command(
        sandbox,
        docker_argv,
        cwd=".",
        timeout_seconds=timeout_seconds,
        approved=True,
        allowlist=_DOCKER_COMMAND_ALLOWLIST,
        _cancel_event=cancel_event,
    )
    if result.error_code in {"cancelled", "timeout"}:
        cleanup = builtins.run_command(
            sandbox,
            ["docker", "rm", "-f", container_name],
            cwd=".",
            timeout_seconds=10,
            approved=True,
            allowlist=_DOCKER_COMMAND_ALLOWLIST,
        )
        result.metadata = {
            **dict(result.metadata),
            "docker_cleanup_succeeded": cleanup.ok,
        }
        if not cleanup.ok:
            result.content = (
                result.content
                + "\nDocker 容器清理失败："
                + cleanup.content
            )
    result.metadata = {
        **dict(result.metadata),
        "cwd": relative_cwd,
        "validation_runtime": "docker",
        "validation_image": str(config["image"]),
        "network_disabled": True,
    }
    return result
