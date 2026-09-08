"""Deterministic safeguards around the otherwise dynamic agent loop."""
# 文件职责：负责智能体状态机、模型行动、工具观察与完成判定。
# 逻辑关系：本模块接收上层运行服务的输入，推进智能体执行后把状态、事件或结果返回调用层。

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


# 类职责：封装 GuardDecision 的状态、依赖和领域行为。
# 协作关系：实例由运行服务或相邻节点创建，并在智能体步骤之间传递数据。
@dataclass(frozen=True, slots=True)
class GuardDecision:
    # 变量说明：stop 表示当前步骤使用的 stop 值。
    stop: bool
    # 变量说明：code 表示当前步骤使用的 code 值。
    code: str | None = None
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str | None = None

    # 函数职责：完成 continue 对应的智能体处理。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    @classmethod
    def continue_(cls) -> "GuardDecision":
        return cls(False)


# 类职责：封装 LoopGuard 的状态、依赖和领域行为。
# 协作关系：实例由运行服务或相邻节点创建，并在智能体步骤之间传递数据。
class LoopGuard:
    """Track optional hard limits and deterministic anti-loop signals."""

    # 函数职责：初始化实例依赖和初始状态。
    # 参数关系：max_steps 表示max_steps 集合；max_calls 表示max_calls 集合；identical_limit 表示当前步骤使用的 identical_limit 值；no_progress_limit 表示当前步骤使用的 no_progress_limit 值。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    def __init__(
        self,
        *,
        max_steps: int | None = 0,
        max_calls: int | None = 0,
        identical_limit: int = 3,
        no_progress_limit: int = 4,
    ) -> None:
        if max_steps is not None and max_steps < 0:
            raise ValueError("max_steps 必须大于等于 0")
        if max_calls is not None and max_calls < 0:
            raise ValueError("max_calls 必须大于等于 0")
        if min(identical_limit, no_progress_limit) < 0:
            raise ValueError("重复调用和无进展限制必须大于等于 0")
        # 变量说明：max_steps 表示max_steps 集合。
        self.max_steps = max_steps
        # 变量说明：max_calls 表示max_calls 集合。
        self.max_calls = max_calls
        # 变量说明：identical_limit 表示当前步骤使用的 identical_limit 值。
        self.identical_limit = identical_limit
        # 变量说明：no_progress_limit 表示当前步骤使用的 no_progress_limit 值。
        self.no_progress_limit = no_progress_limit
        # 变量说明：steps 表示steps 集合。
        self.steps = 0
        # 变量说明：calls 表示calls 集合。
        self.calls = 0
        # 变量说明：identical_count 表示当前步骤使用的 identical_count 值。
        self.identical_count = 0
        # 变量说明：no_progress_count 表示当前步骤使用的 no_progress_count 值。
        self.no_progress_count = 0
        # 变量说明：stagnation_recovery_count 表示当前步骤使用的 stagnation_recovery_count 值。
        self.stagnation_recovery_count = 0
        # 变量说明：stagnation_recovery_pending 表示当前步骤使用的 stagnation_recovery_pending 值。
        self.stagnation_recovery_pending = False
        # 变量说明：_last_call_fingerprint 表示当前步骤使用的 _last_call_fingerprint 值。
        self._last_call_fingerprint: str | None = None

    # 函数职责：完成 call_fingerprint 对应的智能体处理。
    # 参数关系：tool_name 表示当前步骤使用的 tool_name 值；arguments 表示arguments 集合。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    @staticmethod
    def call_fingerprint(tool_name: str, arguments: dict[str, Any] | None) -> str:
        # 变量说明：canonical 表示当前步骤使用的 canonical 值。
        canonical = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(f"{tool_name}\0{canonical}".encode("utf-8")).hexdigest()

    # 函数职责：完成 before_step 对应的智能体处理。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    def before_step(self) -> GuardDecision:
        if self.max_steps and self.steps >= self.max_steps:
            return GuardDecision(True, "max_steps", f"已达到最大步骤数 {self.max_steps}")
        self.steps += 1
        return GuardDecision.continue_()

    # 函数职责：完成 before_tool_call 对应的智能体处理。
    # 参数关系：tool_name 表示当前步骤使用的 tool_name 值；arguments 表示arguments 集合。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    def before_tool_call(self, tool_name: str, arguments: dict[str, Any] | None) -> GuardDecision:
        if self.max_calls and self.calls >= self.max_calls:
            return GuardDecision(True, "max_tool_calls", f"已达到最大工具调用数 {self.max_calls}")

        # 变量说明：fingerprint 表示当前步骤使用的 fingerprint 值。
        fingerprint = self.call_fingerprint(tool_name, arguments)
        if fingerprint == self._last_call_fingerprint:
            self.identical_count += 1
        else:
            # 变量说明：_last_call_fingerprint 表示当前步骤使用的 _last_call_fingerprint 值。
            self._last_call_fingerprint = fingerprint
            # 变量说明：identical_count 表示当前步骤使用的 identical_count 值。
            self.identical_count = 1
        self.calls += 1

        if self.identical_limit and self.identical_count >= self.identical_limit:
            return GuardDecision(
                True,
                "repeated_tool_call",
                f"相同工具和参数已连续出现 {self.identical_count} 次",
            )
        return GuardDecision.continue_()

    # Kept as an intuitive alias for callers and tests.
    # 变量说明：record_tool_call 表示当前步骤使用的 record_tool_call 值。
    record_tool_call = before_tool_call

    # 函数职责：记录 progress 对应流程。
    # 参数关系：made_progress 表示made_progress 集合。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    def record_progress(self, made_progress: bool) -> GuardDecision:
        if self.stagnation_recovery_pending:
            # 变量说明：stagnation_recovery_pending 表示当前步骤使用的 stagnation_recovery_pending 值。
            self.stagnation_recovery_pending = False
            if made_progress:
                # 变量说明：no_progress_count 表示当前步骤使用的 no_progress_count 值。
                self.no_progress_count = 0
                return GuardDecision.continue_()
            # 变量说明：no_progress_count 表示当前步骤使用的 no_progress_count 值。
            self.no_progress_count = self.no_progress_limit
            return GuardDecision(
                True,
                "no_progress",
                "恢复轮仍未产生有效进展",
            )
        if made_progress:
            # 变量说明：no_progress_count 表示当前步骤使用的 no_progress_count 值。
            self.no_progress_count = 0
        else:
            self.no_progress_count += 1
        if self.no_progress_limit and self.no_progress_count >= self.no_progress_limit:
            return GuardDecision(
                True,
                "no_progress",
                f"已连续 {self.no_progress_count} 步没有产生有效进展",
            )
        return GuardDecision.continue_()

    # 函数职责：完成 begin_stagnation_recovery 对应的智能体处理。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    def begin_stagnation_recovery(self) -> None:
        """Record one caller-authorized recovery attempt and restart the counter."""

        self.stagnation_recovery_count += 1
        # 变量说明：no_progress_count 表示当前步骤使用的 no_progress_count 值。
        self.no_progress_count = 0
        # 变量说明：stagnation_recovery_pending 表示当前步骤使用的 stagnation_recovery_pending 值。
        self.stagnation_recovery_pending = True

    # 函数职责：完成 snapshot 对应的智能体处理。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    def snapshot(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "calls": self.calls,
            "identical_count": self.identical_count,
            "no_progress_count": self.no_progress_count,
            "stagnation_recovery_count": self.stagnation_recovery_count,
            "stagnation_recovery_pending": self.stagnation_recovery_pending,
            "last_call_fingerprint": self._last_call_fingerprint,
        }

    # 函数职责：完成 restore 对应的智能体处理。
    # 参数关系：snapshot 表示当前步骤使用的 snapshot 值。
    # 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
    def restore(self, snapshot: dict[str, Any] | None) -> None:
        """Restore counters from a persisted approval or runtime snapshot."""

        if not snapshot:
            return
        # 变量说明：steps 表示steps 集合。
        steps = max(int(snapshot.get("steps", 0)), 0)
        # 变量说明：calls 表示calls 集合。
        calls = max(int(snapshot.get("calls", 0)), 0)
        # 变量说明：steps 表示steps 集合。
        self.steps = min(steps, self.max_steps) if self.max_steps else steps
        # 变量说明：calls 表示calls 集合。
        self.calls = min(calls, self.max_calls) if self.max_calls else calls
        # 变量说明：restored_identical 表示当前步骤使用的 restored_identical 值。
        restored_identical = max(int(snapshot.get("identical_count", 0)), 0)
        # 变量说明：restored_progress 表示restored_progress 集合。
        restored_progress = max(int(snapshot.get("no_progress_count", 0)), 0)
        # 变量说明：identical_count 表示当前步骤使用的 identical_count 值。
        self.identical_count = min(restored_identical, self.identical_limit) if self.identical_limit else restored_identical
        # 变量说明：no_progress_count 表示当前步骤使用的 no_progress_count 值。
        self.no_progress_count = min(restored_progress, self.no_progress_limit) if self.no_progress_limit else restored_progress
        # 变量说明：stagnation_recovery_count 表示当前步骤使用的 stagnation_recovery_count 值。
        self.stagnation_recovery_count = max(
            int(snapshot.get("stagnation_recovery_count", 0)),
            0,
        )
        # 变量说明：stagnation_recovery_pending 表示当前步骤使用的 stagnation_recovery_pending 值。
        self.stagnation_recovery_pending = bool(
            snapshot.get("stagnation_recovery_pending", False)
        )
        # 变量说明：fingerprint 表示当前步骤使用的 fingerprint 值。
        fingerprint = snapshot.get("last_call_fingerprint")
        # 变量说明：_last_call_fingerprint 表示当前步骤使用的 _last_call_fingerprint 值。
        self._last_call_fingerprint = str(fingerprint) if fingerprint else None
