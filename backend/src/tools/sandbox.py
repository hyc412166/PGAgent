"""Filesystem boundary enforcement for a PGAgent workspace."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 sandbox 子模块。
# 逻辑关系：上层通过 tools/sandbox.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from pathlib import Path


# 类职责：定义 SandboxViolation 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SandboxViolation(ValueError):
    """Raised when a path attempts to escape its assigned workspace."""


# 类职责：定义 WorkspaceSandbox 在本领域中的数据与行为。
class WorkspaceSandbox:
    """Resolve all file access beneath one immutable workspace root."""

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：root 表示处理范围的根目录。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self, root: str | Path) -> None:
        # 变量说明：root 表示处理范围的根目录。
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # 函数职责：完成 resolve 对应的业务处理。
    # 参数关系：relative_path 表示relative_path 对应的文件系统位置；must_exist 表示当前步骤使用的 must_exist 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def resolve(self, relative_path: str | Path = ".", *, must_exist: bool = False) -> Path:
        # 变量说明：candidate_input 表示当前步骤使用的 candidate_input 值。
        candidate_input = Path(relative_path)
        if candidate_input.is_absolute():
            raise SandboxViolation("只允许使用工作区内的相对路径")

        # 变量说明：candidate 表示当前步骤使用的 candidate 值。
        candidate = (self.root / candidate_input).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise SandboxViolation("路径越过了工作区边界") from exc

        if must_exist and not candidate.exists():
            raise FileNotFoundError(f"路径不存在: {relative_path}")
        return candidate

    # 函数职责：完成 relative 对应的业务处理。
    # 参数关系：path 表示当前文件或目录路径。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def relative(self, path: Path) -> str:
        # Display the lexical path, not a symlink's target. Access checks always go
        # through ``resolve`` separately, so an outward symlink can be listed but
        # can never be followed by read/write tools.
        return path.absolute().relative_to(self.root).as_posix() or "."
