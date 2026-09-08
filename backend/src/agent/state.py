# 文件职责：定义 engine 与 loop 共享的可变状态字段，兼容暂停恢复时的部分状态。
"""Mutable state carried through one AgentRuntime execution."""

from __future__ import annotations

from typing import Any, TypedDict


# 运行状态不是业务会话本身；它同时携带模型消息、工具轨迹、压缩记录和终止原因。
# 类职责：封装 RunState 的状态、依赖和领域行为。
# 协作关系：实例由运行服务或相邻节点创建，并在智能体步骤之间传递数据。
class RunState(TypedDict, total=False):
    # status/mode 控制生命周期；messages 是模型对话，events 是向外发布的运行事件。
    # 变量说明：status 表示status 集合。
    status: str
    # 变量说明：mode 表示当前步骤使用的 mode 值。
    mode: str
    # 变量说明：messages 表示模型消息序列。
    messages: list[dict[str, Any]]
    # 变量说明：events 表示events 集合。
    events: list[dict[str, Any]]
    # 输出、错误、停止原因与待审批请求共同描述当前运行是否以及为何结束。
    # 变量说明：output 表示当前步骤使用的 output 值。
    output: str | None
    # 变量说明：error 表示当前异常。
    error: str | None
    # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
    stop_reason: str | None
    # 变量说明：pending_approval 表示当前步骤使用的 pending_approval 值。
    pending_approval: dict[str, Any] | None
    # 当前轮进展和历史观察用于停滞判断；usage 汇总调用开销。
    # 变量说明：current_made_progress 表示current_made_progress 集合。
    current_made_progress: bool
    # 变量说明：seen_observations 表示seen_observations 集合。
    seen_observations: list[str]
    # 变量说明：usage 表示当前步骤使用的 usage 值。
    usage: dict[str, Any]
    # 上下文预算、压缩状态和外置内容引用用于构造下一次模型请求。
    # 变量说明：context 表示本轮模型上下文。
    context: dict[str, Any]
    # 变量说明：compaction_state 表示当前步骤使用的 compaction_state 值。
    compaction_state: dict[str, Any]
    # 变量说明：context_artifact_refs 表示context_artifact_refs 集合。
    context_artifact_refs: list[dict[str, Any]]
    # 对话增量供持久化，verification_trace 保留完成校验所需的未裁剪工具链。
    # 变量说明：transcript_delta 表示当前步骤使用的 transcript_delta 值。
    transcript_delta: list[dict[str, Any]]
    # 变量说明：verification_trace 表示当前步骤使用的 verification_trace 值。
    verification_trace: list[dict[str, Any]]
    # 压缩、溢出重试和完成校验次数是运行内计数，不代表用户任务完成程度。
    # 变量说明：compaction_count 表示当前步骤使用的 compaction_count 值。
    compaction_count: int
    # 变量说明：context_overflow_retries 表示context_overflow_retries 集合。
    context_overflow_retries: int
    # 变量说明：completion_verification_attempts 表示completion_verification_attempts 集合。
    completion_verification_attempts: int
    # 验收报告和记忆引用进入最终结果；缓存键绑定提示词缓存，恢复提示只用于停滞恢复轮。
    # 变量说明：acceptance_report 表示当前步骤使用的 acceptance_report 值。
    acceptance_report: dict[str, Any]
    # 变量说明：memory_citation 表示当前步骤使用的 memory_citation 值。
    memory_citation: dict[str, Any]
    # 变量说明：prompt_cache_key 表示当前步骤使用的 prompt_cache_key 值。
    prompt_cache_key: str
    # 变量说明：stagnation_recovery_prompt 表示当前步骤使用的 stagnation_recovery_prompt 值。
    stagnation_recovery_prompt: str
    # 模型服务端托管工具调用数量，与本地注册工具计数分开记录。
    # 变量说明：hosted_tool_calls 表示hosted_tool_calls 集合。
    hosted_tool_calls: int
