"""Provider-callable helpers that emit structured review and debug evidence."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 tools 子模块。
# 逻辑关系：上层通过 coding/evidence/tools.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import json
from typing import Any

from src.tools.sandbox import WorkspaceSandbox
from src.tools.types import ToolResult


# 函数职责：记录 review_finding 对应的数据或流程。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；severity 表示当前步骤使用的 severity 值；title 表示当前步骤使用的 title 值；path 表示当前文件或目录路径；failure_scenario 表示当前步骤使用的 failure_scenario 值；evidence 表示当前步骤使用的 evidence 值；line 表示当前步骤使用的 line 值；suggested_fix 表示当前步骤使用的 suggested_fix 值；其余参数沿用调用方提供的扩展选项。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def record_review_finding(
    _sandbox: WorkspaceSandbox,
    *,
    severity: str,
    title: str,
    path: str,
    failure_scenario: str,
    evidence: str,
    line: int | None = None,
    suggested_fix: str = "",
    test_gap: str = "",
) -> ToolResult:
    # 变量说明：finding 表示当前步骤使用的 finding 值。
    finding: dict[str, Any] = {
        "kind": "review_finding",
        "severity": severity,
        "title": title,
        "path": path,
        "failure_scenario": failure_scenario,
        "evidence": evidence,
    }
    if line is not None:
        # 变量说明：finding 的索引项 表示该语句创建或更新的目标数据。
        finding["line"] = line
    if suggested_fix:
        # 变量说明：finding 的索引项 表示该语句创建或更新的目标数据。
        finding["suggested_fix"] = suggested_fix
    if test_gap:
        # 变量说明：finding 的索引项 表示该语句创建或更新的目标数据。
        finding["test_gap"] = test_gap
    return ToolResult(
        "review_finding",
        True,
        json.dumps(finding, ensure_ascii=False),
        metadata={"workflow_evidence": finding},
    )


# 函数职责：记录 debug_evidence 对应的数据或流程。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；stage 表示当前步骤使用的 stage 值；summary 表示当前步骤使用的 summary 值；status 表示当前对象或运行的状态；path 表示当前文件或目录路径；line 表示当前步骤使用的 line 值；command 表示当前步骤使用的 command 值；details 表示当前流程使用的 details 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def record_debug_evidence(
    _sandbox: WorkspaceSandbox,
    *,
    stage: str,
    summary: str,
    status: str,
    path: str = "",
    line: int | None = None,
    command: str = "",
    details: str = "",
) -> ToolResult:
    # 变量说明：evidence 表示当前步骤使用的 evidence 值。
    evidence: dict[str, Any] = {
        "kind": "debug_evidence",
        "stage": stage,
        "summary": summary,
        "status": status,
    }
    if path:
        # 变量说明：evidence 的索引项 表示该语句创建或更新的目标数据。
        evidence["path"] = path
    if line is not None:
        # 变量说明：evidence 的索引项 表示该语句创建或更新的目标数据。
        evidence["line"] = line
    if command:
        # 变量说明：evidence 的索引项 表示该语句创建或更新的目标数据。
        evidence["command"] = command
    if details:
        # 变量说明：evidence 的索引项 表示该语句创建或更新的目标数据。
        evidence["details"] = details
    return ToolResult(
        "debug_evidence",
        True,
        json.dumps(evidence, ensure_ascii=False),
        metadata={"workflow_evidence": evidence},
    )
