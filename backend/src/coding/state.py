"""Run-scoped coding evidence captured from successful tool invocations."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 state 子模块。
# 逻辑关系：上层通过 coding/state.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from src.tools.hooks import HookContext
from src.tools.invocation import ToolInvocation
from src.tools.types import ToolResult


# 函数职责：完成 mapping_items 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _mapping_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


# 类职责：集中描述 CodingSessionState 的流程状态。
@dataclass(slots=True)
class CodingSessionState:
    """Small durable ledger; patch bodies and command output stay in normal tool results."""

    # 变量说明：changes 表示当前流程使用的 changes 集合。
    changes: list[dict[str, Any]] = field(default_factory=list)
    # 变量说明：validations 表示当前流程使用的 validations 集合。
    validations: list[dict[str, Any]] = field(default_factory=list)
    # 变量说明：revision 表示当前步骤使用的 revision 值。
    revision: int = 0

    # 函数职责：完成 restore 对应的业务处理。
    # 参数关系：value 表示当前字段或计算值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def restore(cls, value: Mapping[str, Any] | None) -> "CodingSessionState":
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = dict(value or {})
        # 变量说明：changes 表示当前流程使用的 changes 集合。
        changes = _mapping_items(payload.get("changes"))
        # 变量说明：validations 表示当前流程使用的 validations 集合。
        validations = _mapping_items(payload.get("validations"))
        # 变量说明：revision 表示当前步骤使用的 revision 值。
        revision = max([
            int(payload.get("revision") or 0),
            *(int(item.get("revision") or 0) for item in (*changes, *validations)),
        ])
        return cls(changes=changes, validations=validations, revision=revision)

    # 函数职责：异步完成 after_invoke 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值；result 表示本步骤产生的结果；_context 表示当前步骤使用的 _context 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def after_invoke(
        self,
        invocation: ToolInvocation,
        result: ToolResult,
        _context: HookContext,
    ) -> ToolResult:
        # 变量说明：change_set 表示当前步骤使用的 change_set 值。
        change_set = result.metadata.get("change_set")
        if result.changed and not isinstance(change_set, Mapping):
            # 变量说明：path 表示当前文件或目录路径。
            path = str(result.metadata.get("path") or invocation.arguments.get("path") or "").strip()
            if path:
                # 变量说明：operation 表示当前步骤使用的 operation 值。
                operation = str(result.metadata.get("operation") or "").strip() or {
                    "delete": "delete",
                    "write": "update",
                    "write_file": "update",
                    "edit": "update",
                    "edit_file": "update",
                }.get(invocation.wire_name)
                if operation:
                    # 变量说明：change_set 表示当前步骤使用的 change_set 值。
                    change_set = {
                        "status": "observed",
                        "source": "file_tool",
                        "file_count": 1,
                        "files": [{"path": path.replace("\\", "/"), "operation": operation}],
                    }
        if result.changed and isinstance(change_set, Mapping):
            self.revision += 1
            self.changes.append({
                "sequence": len(self.changes) + 1,
                "revision": self.revision,
                "call_id": invocation.call_id,
                "tool": invocation.wire_name,
                **dict(change_set),
            })

        # 变量说明：validation 表示当前步骤使用的 validation 值。
        validation = result.metadata.get("validation")
        if isinstance(validation, Mapping):
            self.revision += 1
            self.validations.append({
                "sequence": len(self.validations) + 1,
                "revision": self.revision,
                "call_id": invocation.call_id,
                "tool": invocation.wire_name,
                **dict(validation),
            })
        return result

    # 函数职责：完成 snapshot 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def snapshot(self) -> dict[str, Any]:
        return {
            "changes": [dict(item) for item in self.changes],
            "validations": [dict(item) for item in self.validations],
            "revision": self.revision,
        }

    # 函数职责：完成 prompt_summary 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def prompt_summary(self) -> str:
        if not self.changes and not self.validations:
            return ""
        # 变量说明：touched 表示当前步骤使用的 touched 值。
        touched: list[str] = []
        for record in self.changes:
            for item in record.get("files") or []:
                if isinstance(item, Mapping):
                    # 变量说明：path 表示当前文件或目录路径。
                    path = str(item.get("path") or "").strip()
                    if path and path not in touched:
                        touched.append(path)
        # 变量说明：lines 表示当前流程使用的 lines 集合。
        lines = ["## Current coding evidence"]
        if touched:
            lines.append("Changed paths: " + ", ".join(touched))
        # 变量说明：latest_change_revision 表示当前步骤使用的 latest_change_revision 值。
        latest_change_revision = max(
            (int(item.get("revision") or 0) for item in self.changes),
            default=0,
        )
        # 变量说明：latest_validation_revision 表示当前步骤使用的 latest_validation_revision 值。
        latest_validation_revision = max(
            (int(item.get("revision") or 0) for item in self.validations),
            default=0,
        )
        if self.validations and latest_validation_revision > latest_change_revision:
            # 变量说明：latest 表示当前步骤使用的 latest 值。
            latest = self.validations[-1]
            lines.append(
                "Latest validation: "
                f"{latest.get('kind', 'other')} status={latest.get('status', 'unknown')} "
                f"cwd={latest.get('cwd', '.')} exit_code={latest.get('exit_code')}"
            )
        elif self.validations and self.changes:
            lines.append("Validation status: stale (changes were made after the latest validation)")
        elif self.changes:
            lines.append("Validation status: not_run")
        return "\n".join(lines)
