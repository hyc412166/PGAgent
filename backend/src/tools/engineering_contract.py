"""Provider contracts for engineering workflow evidence tools."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 engineering_contract 子模块。
# 逻辑关系：上层通过 tools/engineering_contract.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from typing import Any


# 变量说明：ENGINEERING_TOOL_SCHEMAS 表示当前流程使用的 ENGINEERING_TOOL_SCHEMAS 集合。
ENGINEERING_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "review_finding": {
        "description": "Record one actionable code-review finding with a concrete failure scenario.",
        "parameters": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["critical", "important", "minor"]},
                "title": {"type": "string"},
                "path": {"type": "string"},
                "line": {"type": "integer", "minimum": 1},
                "failure_scenario": {"type": "string"},
                "evidence": {"type": "string"},
                "suggested_fix": {"type": "string"},
                "test_gap": {"type": "string"},
            },
            "required": ["severity", "title", "path", "failure_scenario", "evidence"],
        },
    },
    "debug_evidence": {
        "description": "Record one debugging observation, hypothesis, root cause, or regression result.",
        "parameters": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": [
                        "symptom",
                        "reproduction",
                        "observation",
                        "hypothesis",
                        "root_cause",
                        "regression",
                    ],
                },
                "summary": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [
                        "confirmed",
                        "supported",
                        "unconfirmed",
                        "disproved",
                        "passed",
                        "failed",
                    ],
                },
                "path": {"type": "string"},
                "line": {"type": "integer", "minimum": 1},
                "command": {"type": "string"},
                "details": {"type": "string"},
            },
            "required": ["stage", "summary", "status"],
        },
    },
}

# 变量说明：ENGINEERING_TOOL_NAMES 表示当前流程使用的 ENGINEERING_TOOL_NAMES 集合。
ENGINEERING_TOOL_NAMES = tuple(ENGINEERING_TOOL_SCHEMAS)
