"""Serialize and restore the durable continuation state of an AgentRuntime."""
# 文件职责：负责运行创建、恢复、流式传输和生命周期中的 continuation 子模块。
# 逻辑关系：上层通过 runs/continuation.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from typing import Any

from src.agent import AgentRuntime, RunOutcome, normalize_usage
from src.context.compaction import _json_safe
from src.tools.registry import ToolRegistry


# 类职责：定义 RunContinuationCodec 在本领域中的数据与行为。
class RunContinuationCodec:
    # 函数职责：完成 capture_runtime_binding 对应的业务处理。
    # 参数关系：base 表示当前步骤使用的 base 值；runtime 表示当前步骤使用的 runtime 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def capture_runtime_binding(
        base: dict[str, Any],
        runtime: AgentRuntime,
    ) -> dict[str, Any]:
        return {
            **dict(base),
            **runtime.tool_registry.runtime_state(),
        }

    # 函数职责：完成 capture_registry_binding 对应的业务处理。
    # 参数关系：base 表示当前步骤使用的 base 值；registry 表示当前步骤使用的 registry 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def capture_registry_binding(
        base: dict[str, Any],
        registry: ToolRegistry,
    ) -> dict[str, Any]:
        return {
            **dict(base),
            **registry.runtime_state(),
        }

    # 函数职责：完成 snapshot 对应的业务处理。
    # 参数关系：outcome 表示当前步骤使用的 outcome 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def snapshot(outcome: RunOutcome) -> dict[str, Any]:
        # 变量说明：messages 表示发送给模型或客户端的消息序列。
        messages = [] if outcome.stop_reason == "acceptance_failed" else outcome.messages
        return _json_safe({
            "status": outcome.status,
            "output": outcome.output,
            "messages": messages,
            "events": outcome.events,
            "steps": outcome.steps,
            "tool_calls": outcome.tool_calls,
            "mode": outcome.mode,
            "stop_reason": outcome.stop_reason,
            "error": outcome.error,
            "pending_approval": outcome.pending_approval,
            "guard_snapshot": outcome.guard_snapshot,
            "usage": outcome.usage,
            "active_elapsed_seconds": outcome.active_elapsed_seconds,
            "runtime_binding": outcome.runtime_binding,
            "compaction_state": outcome.compaction_state,
            "artifact_refs": outcome.artifact_refs,
            "transcript_delta": outcome.transcript_delta,
            "verification_trace": outcome.verification_trace,
            "acceptance_report": dict(outcome.acceptance_report),
            "completion_verification_attempts": outcome.completion_verification_attempts,
            "memory_citation": outcome.memory_citation,
        })

    # 函数职责：完成 restore 对应的业务处理。
    # 参数关系：payload 表示跨层传递的数据载荷。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def restore(payload: dict[str, Any]) -> RunOutcome:
        return RunOutcome(
            status=str(payload.get("status") or "failed"),
            output=payload.get("output"),
            messages=list(payload.get("messages") or []),
            events=list(payload.get("events") or []),
            steps=int(payload.get("steps") or 0),
            tool_calls=int(payload.get("tool_calls") or 0),
            mode=str(payload.get("mode") or "auto"),
            stop_reason=payload.get("stop_reason"),
            error=payload.get("error"),
            pending_approval=payload.get("pending_approval"),
            guard_snapshot=dict(payload.get("guard_snapshot") or {}),
            usage=normalize_usage(payload.get("usage")),
            active_elapsed_seconds=max(0.0, float(payload.get("active_elapsed_seconds") or 0.0)),
            runtime_binding=dict(payload.get("runtime_binding") or {}),
            compaction_state=dict(payload.get("compaction_state") or {}),
            artifact_refs=[dict(item) for item in payload.get("artifact_refs") or [] if isinstance(item, dict)],
            transcript_delta=(
                [dict(item) for item in payload.get("transcript_delta") or [] if isinstance(item, dict)]
                if "transcript_delta" in payload
                else None
            ),
            verification_trace=[
                dict(item) for item in payload.get("verification_trace") or [] if isinstance(item, dict)
            ],
            acceptance_report=dict(payload.get("acceptance_report") or {}),
            completion_verification_attempts=max(
                0,
                int(payload.get("completion_verification_attempts") or 0),
            ),
            memory_citation=dict(payload.get("memory_citation") or {}),
        )
