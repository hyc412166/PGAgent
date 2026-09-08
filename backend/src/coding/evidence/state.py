"""Run-scoped structured evidence preserved across approval and recovery."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 state 子模块。
# 逻辑关系：上层通过 coding/evidence/state.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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


# 类职责：集中描述 WorkflowEvidenceState 的流程状态。
@dataclass(slots=True)
class WorkflowEvidenceState:
    """Compact ledger of model-recorded review findings and debug evidence."""

    # 变量说明：review_findings 表示当前流程使用的 review_findings 集合。
    review_findings: list[dict[str, Any]] = field(default_factory=list)
    # 变量说明：debug_evidence 表示当前步骤使用的 debug_evidence 值。
    debug_evidence: list[dict[str, Any]] = field(default_factory=list)
    # 变量说明：sequence 表示当前步骤使用的 sequence 值。
    sequence: int = 0

    # 函数职责：完成 restore 对应的业务处理。
    # 参数关系：value 表示当前字段或计算值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def restore(cls, value: Mapping[str, Any] | None) -> "WorkflowEvidenceState":
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = dict(value or {})
        # 变量说明：findings 表示当前流程使用的 findings 集合。
        findings = _mapping_items(payload.get("review_findings"))
        # 变量说明：evidence 表示当前步骤使用的 evidence 值。
        evidence = _mapping_items(payload.get("debug_evidence"))
        # 变量说明：sequence 表示当前步骤使用的 sequence 值。
        sequence = max([
            int(payload.get("sequence") or 0),
            *(int(item.get("sequence") or 0) for item in (*findings, *evidence)),
        ])
        return cls(review_findings=findings, debug_evidence=evidence, sequence=sequence)

    # 函数职责：异步完成 after_invoke 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值；result 表示本步骤产生的结果；_context 表示当前步骤使用的 _context 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def after_invoke(
        self,
        invocation: ToolInvocation,
        result: ToolResult,
        _context: HookContext,
    ) -> ToolResult:
        # 变量说明：evidence 表示当前步骤使用的 evidence 值。
        evidence = result.metadata.get("workflow_evidence")
        if not result.ok or not isinstance(evidence, Mapping):
            return result
        self.sequence += 1
        # 变量说明：record 表示当前步骤使用的 record 值。
        record = {
            "sequence": self.sequence,
            "call_id": invocation.call_id,
            **dict(evidence),
        }
        if evidence.get("kind") == "review_finding":
            self.review_findings.append(record)
        elif evidence.get("kind") == "debug_evidence":
            self.debug_evidence.append(record)
        return result

    # 函数职责：完成 snapshot 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def snapshot(self) -> dict[str, Any]:
        return {
            "review_findings": [dict(item) for item in self.review_findings],
            "debug_evidence": [dict(item) for item in self.debug_evidence],
            "sequence": self.sequence,
        }

    # 函数职责：完成 prompt_summary 对应的业务处理。
    # 参数关系：profile_id 表示profile 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def prompt_summary(self, profile_id: str) -> str:
        if profile_id == "review" and self.review_findings:
            # 变量说明：lines 表示当前流程使用的 lines 集合。
            lines = ["## Recorded review findings"]
            for item in self.review_findings:
                # 变量说明：location 表示当前步骤使用的 location 值。
                location = str(item.get("path") or "")
                if item.get("line") is not None:
                    location += f":{item['line']}"
                lines.append(
                    f"- [{item.get('severity', 'unknown')}] {item.get('title', '')} "
                    f"({location or 'location not recorded'})"
                )
                lines.append(f"  Failure scenario: {item.get('failure_scenario', '')}")
                lines.append(f"  Evidence: {item.get('evidence', '')}")
                if item.get("suggested_fix"):
                    lines.append(f"  Suggested fix: {item['suggested_fix']}")
                if item.get("test_gap"):
                    lines.append(f"  Test gap: {item['test_gap']}")
            return "\n".join(lines)
        if profile_id == "debug" and self.debug_evidence:
            # 变量说明：lines 表示当前流程使用的 lines 集合。
            lines = ["## Recorded debug evidence"]
            for item in self.debug_evidence:
                lines.append(
                    f"- {item.get('stage', 'observation')} "
                    f"status={item.get('status', 'unknown')}: {item.get('summary', '')}"
                )
                # 变量说明：location 表示当前步骤使用的 location 值。
                location = str(item.get("path") or "")
                if item.get("line") is not None:
                    location += f":{item['line']}"
                if location:
                    lines.append(f"  Location: {location}")
                if item.get("command"):
                    lines.append(f"  Command: {item['command']}")
                if item.get("details"):
                    lines.append(f"  Details: {item['details']}")
            return "\n".join(lines)
        return ""
