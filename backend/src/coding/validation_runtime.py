"""Explicit execution runtimes for project validation commands."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 validation_runtime 子模块。
# 逻辑关系：上层通过 coding/validation_runtime.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import threading
from pathlib import PurePosixPath
from typing import Any, Mapping
import uuid

from src.tools import builtins
from src.tools.sandbox import WorkspaceSandbox
from src.tools.types import ToolResult


# 变量说明：_DOCKER_COMMAND_ALLOWLIST 表示当前步骤使用的 _DOCKER_COMMAND_ALLOWLIST 值。
_DOCKER_COMMAND_ALLOWLIST = frozenset({"docker", "docker.exe"})


# 函数职责：规范化 validation_runtime 对应的数据或流程。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def normalize_validation_runtime(value: Mapping[str, Any] | None) -> dict[str, Any]:
    # 变量说明：raw 表示当前步骤使用的 raw 值。
    raw = dict(value or {})
    # 变量说明：kind 表示当前步骤使用的 kind 值。
    kind = str(raw.get("kind") or "local").strip().lower()
    if kind == "local":
        return {"kind": "local"}
    if kind != "docker":
        raise ValueError(f"unsupported validation runtime: {kind}")
    # 变量说明：image 表示当前步骤使用的 image 值。
    image = str(raw.get("image") or "").strip()
    if not image:
        raise ValueError("Docker validation runtime requires an image")
    if image.startswith("-"):
        raise ValueError("Docker validation image must not start with '-'")
    if len(image) > 500 or any(character.isspace() or ord(character) < 32 for character in image):
        raise ValueError("invalid Docker validation image")
    return {"kind": "docker", "image": image}


# 函数职责：执行 in_validation_runtime 对应的数据或流程。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；command 表示当前步骤使用的 command 值；runtime 表示当前步骤使用的 runtime 值；cwd 表示当前步骤使用的 cwd 值；timeout_seconds 表示当前流程使用的 timeout_seconds 集合；approved 表示当前步骤使用的 approved 值；cancel_event 表示当前步骤使用的 cancel_event 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：config 表示当前生效的配置。
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
        # 变量说明：working_directory 表示当前步骤使用的 working_directory 值。
        working_directory = sandbox.resolve(cwd, must_exist=True)
        if not working_directory.is_dir():
            return ToolResult("validate", False, "cwd is not a directory", error_code="not_directory")
        # 变量说明：relative_cwd 表示当前步骤使用的 relative_cwd 值。
        relative_cwd = sandbox.relative(working_directory)
        # 变量说明：inner_argv 表示当前步骤使用的 inner_argv 值。
        inner_argv = builtins.parse_command_argv(command)
    except (ValueError, OSError) as exc:
        return ToolResult("validate", False, str(exc), error_code="command_error")

    # SWE-bench images install the checked-out project at /testbed. Mounting
    # the candidate there preserves their dependency and editable-import paths.
    # 变量说明：container_root 表示当前步骤使用的 container_root 值。
    container_root = PurePosixPath("/testbed")
    # 变量说明：container_cwd 表示当前步骤使用的 container_cwd 值。
    container_cwd = container_root.joinpath(*PurePosixPath(relative_cwd).parts).as_posix()
    # 变量说明：container_name 表示当前步骤使用的 container_name 值。
    container_name = f"pgagent-validation-{uuid.uuid4().hex}"
    # 变量说明：docker_argv 表示当前步骤使用的 docker_argv 值。
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
    # 变量说明：result 表示本步骤产生的结果。
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
        # 变量说明：cleanup 表示当前步骤使用的 cleanup 值。
        cleanup = builtins.run_command(
            sandbox,
            ["docker", "rm", "-f", container_name],
            cwd=".",
            timeout_seconds=10,
            approved=True,
            allowlist=_DOCKER_COMMAND_ALLOWLIST,
        )
        # 变量说明：metadata 表示当前步骤使用的 metadata 值。
        result.metadata = {
            **dict(result.metadata),
            "docker_cleanup_succeeded": cleanup.ok,
        }
        if not cleanup.ok:
            # 变量说明：content 表示待处理或返回的正文内容。
            result.content = (
                result.content
                + "\nDocker 容器清理失败："
                + cleanup.content
            )
    # 变量说明：metadata 表示当前步骤使用的 metadata 值。
    result.metadata = {
        **dict(result.metadata),
        "cwd": relative_cwd,
        "validation_runtime": "docker",
        "validation_image": str(config["image"]),
        "network_disabled": True,
    }
    return result
