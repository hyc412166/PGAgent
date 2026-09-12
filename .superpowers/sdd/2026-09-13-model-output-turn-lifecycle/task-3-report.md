# Task 3 报告：Response ledger 与 Turn 状态机

## 实现

- 新增 `TurnLedger`、`TurnDecision`、`TurnStatus` 和 `LocalToolStatus`，按 response/item 顺序记录 Assistant、local tool、hosted tool 与本地结果提交状态。
- `phase` 只保留展示语义，不参与完成判断；显式 `end_turn=false` 产生结构化 follow-up，`true/unknown` 在其他闭环条件满足时允许终态。
- 本地调用只能来自 `NormalizedModelResponse` 中已完成的 `LocalToolCallItem`。`take_local_calls()` 对同一调用只返回一次，真实 dispatch 前逐项标记 running，结果写入 provider transcript 后才标记 `result_committed`。
- hosted tool 单独计数，不进入 local drain，也不要求本地 `ToolResult`；local/hosted 混合 response 只按 local 数量闭环。
- adapter 的 `_pgagent_normalized_response` sidecar 成为 engine 新路径的结构化事实源；旧 mapping 中冲突的 content/tool_calls 不会覆盖 sidecar。直接返回 `ModelTurn` 的旧调用方会投影到同一 ledger 形态，继续兼容现有 transcript 与 LoopGuard。
- Assistant 工具前正文继续进入 Assistant transcript；新 sidecar 路径不再把该正文另发为 `thought_summary`。旧 completion verifier 分支按任务边界保留，等待 Task 4 删除。
- `RunState` 增加运行内 `output_ledger`；`loop.py` 使用同一 `TurnStatus` 值推进 acting/observing。

## TDD 证据

### RED

首次状态机测试在生产模块不存在时失败：

```text
E:\anaconda3\envs\agent_dock\python.exe -m pytest tests/test_turn_completion.py ... -q
```

关键输出：`ModuleNotFoundError: No module named 'src.agent.turn'`。

import-order 回归先复现 `src.agent -> engine -> src.model -> protocols.common -> src.agent.normalize_usage` 初始化环：

```text
ImportError: cannot import name 'normalize_usage' from partially initialized module 'src.agent'
```

修复方式为 `turn.py` 使用 `TYPE_CHECKING`，engine 仅在 `ModelTurn` 转换方法内延迟导入 output 叶类型；未修改 model 门面或搬移公共函数。

匿名 response 作用域测试先复现第二个 `response_id=None/item_id=message-0` 被错误跨 response 判重：

```text
ValueError: duplicate item id: message-0
```

修复后匿名 response 使用账本内 response 序号建立 item 判重作用域。

调度状态测试先以缺失 `local_status()` 失败；实现后确认 take 只原子预留整批，尚未真实 dispatch 的 remaining calls 保持 `scheduled`，避免审批暂停时误标为 running。

### GREEN

Task 3 聚焦：

```text
E:\anaconda3\envs\agent_dock\python.exe -m pytest tests/test_turn_completion.py tests/test_loop_guard.py -q
```

结果：`64 passed`。

较宽 engine/lifecycle 兼容回归：

```text
E:\anaconda3\envs\agent_dock\python.exe -m pytest tests/test_turn_completion.py tests/test_loop_guard.py tests/test_completion_acceptance.py tests/test_background_jobs.py tests/test_run_service.py -q
```

结果：`141 passed`。

协议 sidecar 兼容回归：

```text
E:\anaconda3\envs\agent_dock\python.exe -m pytest tests/test_model_output.py tests/test_model_protocol_output.py tests/test_model_gateway.py tests/test_model_responses.py -q
```

结果：`71 passed`。

额外验证：双向新鲜子进程 `import src.agent; import src.model` / `import src.model; import src.agent` 共 `2 passed`；`py_compile` 和 `git diff --check` 通过。

## 文件

- `backend/src/agent/turn.py`
- `backend/src/agent/state.py`
- `backend/src/agent/loop.py`
- `backend/src/agent/engine.py`
- `backend/tests/test_turn_completion.py`
- `backend/tests/test_loop_guard.py`
- `.superpowers/sdd/2026-09-13-model-output-turn-lifecycle/task-3-report.md`

## 自审与风险

- 状态机不检查候选文案，也不把 `commentary/final_answer` 当完成证据；结构化测试对三种 phase 使用同一完成预期。
- 同一规范化 Turn 内重复 local call id 在第二次副作用调度前被拒绝；结果工具名错配、未调度提交和重复提交均显式失败。
- 顺序执行进入审批时，当前调用为 `running` 且上层状态为 `awaiting_approval`，尚未触发的 remaining calls 保持 `scheduled`；本次 runtime 内不会被第二次 take。
- ledger 的快照序列化与审批恢复所有权属于 Task 4。本任务只保存运行内对象；Task 4 必须连同 scheduled/running/result_committed、taken 集合和精确 pending call 一起恢复，不能自动重试状态不明确的副作用调用。
- 为兼容历史 `ModelTurn` 测试中跨 response 复用 call id 的行为，旧 `ModelTurn` 投影每个 response 使用独立 ledger，重复行为仍由既有 LoopGuard 截止；adapter sidecar 新路径在整个 Turn 内严格拒绝重复 call id。
- completion verifier、持久终态发布顺序和新 SSE/RunEvent 不属于 Task 3，分别保留给 Task 4/5；本任务没有修改 lifecycle/delivery 文件。

## Review 修复

独立审查复现的三个 Important 问题已按 TDD 修复：

1. `take_local_calls()` 原先在 engine 检查 response 状态之前执行，使 `in_progress/unknown` response 中的 local item 可能越过完成边界。本轮先用 ledger 与 engine 测试复现；现在未完成 response 产生 `model_response_incomplete` 失败决定，且 take 仅在决定为 `draining_tools` 时释放调用。
2. hosted-only/空 Assistant response 原先会产生 `completed`。现在只有非空 Assistant 正文才允许 Turn 完成；空成功输出稳定收敛为 `failed + empty_model_output`，不发布虚假 `run_completed`。带 local tool 的 response 仍先 drain，显式 `end_turn=false` 仍可请求 follow-up。
3. 结果提交原先把 invocation 的 `call.name` 同时作为预期名和实际名。现在 engine 先比较真实 `ToolResult.tool_name` 与 invocation 名称；错配返回可观察的 `ToolResultMismatch/tool_result_mismatch`，不提交 ledger、不构造 tool transcript，也不发起后续模型调用。名称匹配后，ledger 使用真实结果名完成单次 commit。

审批批次回归同时确认：`take_local_calls()` 只预留批次；首个调用真实 dispatch 后为 `running`，未 dispatch 的 remaining calls 保持 `scheduled`。

Review RED（6 个聚焦场景）关键结果：`5 failed, 1 passed`。失败分别为未完成 response 返回 acting 并释放调用、hosted-only 被判 completed、engine 执行未完成 local item、空 hosted response 发布完成、错配 ToolResult 被接受。

Review GREEN：同一 6 个场景 `6 passed`；完整 Task 3 聚焦 `70 passed`；engine/lifecycle 较宽兼容回归 `147 passed`。

## Review 修复 round 2/5

复审故障注入确认：engine 原先在 `_prepare_tool_result_message()` 构造 observation 之前就调用 `commit_local_result()`。当 transcript 构造抛出 `TypeError` 时，调用并未进入 `messages/transcript_delta`，ledger 却已变成 `result_committed`。

RED：新增真实 runtime 回归，将 `_prepare_tool_result_message()` 替换为抛出 `TypeError("transcript construction failed")` 的故障点，并检查同一实际 ledger。结果 `1 failed`：预期 `running`，实际为 `result_committed`。

最小修复：保留上一轮的真实 `ToolResult.tool_name` 匹配校验；仅把 `commit_local_result(call.id, tool_name=result.tool_name)` 移到 tool observation 已成功构造、追加到 `messages`，并写入 `transcript_delta/verification_trace` 之后。构造或追加过程中的任何异常都不会留下假提交。

GREEN：故障注入与名称错配相邻测试 `2 passed`；完整 Task 3 聚焦 `71 passed`；engine/lifecycle 必要兼容回归 `148 passed`；`py_compile` 与 `git diff --check` 通过。

修改文件：`backend/src/agent/engine.py`、`backend/tests/test_loop_guard.py` 与本报告。自审确认没有改变工具执行、审批、事件或 transcript 内容，只修正 ledger 提交时序；未修改 lifecycle/delivery、历史日志、spec 或 plan。
