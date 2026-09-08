"""Agent execution loop, completion checks, retry behavior, and guards."""
# 文件职责：负责智能体状态机、模型行动、工具观察与完成判定。
# 逻辑关系：本模块接收上层运行服务的输入，推进智能体执行后把状态、事件或结果返回调用层。

from .completion import CompletionDecision, DeterministicAcceptanceReport, DeterministicCheck, decide_deterministic_completion, verify_deterministic_completion
from .engine import AgentRuntime, ModelToolCall, ModelTurn, RunOutcome, RuntimeConfig, empty_usage, merge_usage, normalize_usage
from .errors import APIErrorKind, call_with_retry, classify_api_error
from .guards import GuardDecision, LoopGuard
from .step_context import AgentStepContext

# 变量说明：__all__ 表示当前步骤使用的 __all__ 值。
__all__ = [
    "APIErrorKind", "AgentRuntime", "AgentStepContext", "CompletionDecision", "DeterministicAcceptanceReport",
    "DeterministicCheck", "GuardDecision", "LoopGuard", "ModelToolCall", "ModelTurn",
    "RunOutcome", "RuntimeConfig", "call_with_retry", "classify_api_error",
    "decide_deterministic_completion", "empty_usage", "merge_usage", "normalize_usage",
    "verify_deterministic_completion",
]
