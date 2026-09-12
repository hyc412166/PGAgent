# Task 2 报告：Responses 与 Chat Completions 协议适配

## 实现

- `responses.consume()` 现在产出 `NormalizedModelResponse`：Responses message、reasoning、local function call 和 hosted tool 分别映射到标准 item；保留 provider 原生 items 供下一轮续接。
- Responses 严格保留 provider 提供的 `phase/end_turn`，缺失时使用 `unknown`；`response.output_item.done` 与 `response.completed` 重复 item 按 provider item id/顺序合并，避免重复调度。
- Responses 断流异常继续携带已经完成的原生 item；未收到 `output_item.done` 的工具不会进入 completed 集合。
- Chat Completions 在 adapter 内聚合 content、reasoning 和 tool fragments；只有 finish boundary 后形成 `LocalToolCallItem`，assistant item 的 phase/end_turn 恒为 `unknown`。
- 两种 adapter 增加 `to_legacy_payload()`，`request.py` 在边界处把标准响应投影回现有 dict，保留旧 `ModelTurn`、续接和回调调用方；同时支持 `on_assistant_item`/`on_item` 回调，旧 `on_delta`、`on_thought_delta` 不变。

## TDD 证据

### RED

命令（`backend` cwd，仓库默认 Python 缺少 litellm）：

```text
python -m pytest tests/test_model_protocol_output.py -q
```

结果：收集阶段 `ModuleNotFoundError: No module named 'litellm'`。

使用仓库现有运行时后，旧 adapter 的预期失败为：

```text
E       AssertionError: assert False
E        +  where False = isinstance(<dict>, NormalizedModelResponse)
```

### GREEN

```text
E:\\anaconda3\\envs\\agent_dock\\python.exe -m pytest tests/test_model_protocol_output.py -q
```

结果：`3 passed`。

兼容回归：

```text
E:\\anaconda3\\envs\\agent_dock\\python.exe -m pytest tests/test_model_gateway.py tests/test_model_responses.py tests/test_model_protocol_output.py -q
```

结果：`51 passed`。

额外验证：`E:\\anaconda3\\envs\\agent_dock\\python.exe -m compileall -q src/model` 通过；`tests/test_loop_guard.py` 结果 `52 passed`；`git diff --check` 通过。

## 文件

- `backend/src/model/protocols/responses.py`
- `backend/src/model/protocols/chat_completions.py`
- `backend/src/model/request.py`
- `backend/tests/test_model_protocol_output.py`

## 自审与风险

- 标准化响应只在协议适配层使用，request 边界继续返回旧 mapping，避免扩大 engine/lifecycle 范围。
- provider reasoning 未进入 Assistant item 回调；仍通过原有 reasoning 字段保留续接语义。
- Chat Completions 没有 phase/end_turn 来源，adapter 不根据文本、finish reason 或工具数量推断，始终为 `unknown`。
- 响应重复 item 以 id（无 id 时以 output index）去重；不同 item id 但重复 call id 仍由 `NormalizedModelResponse` 拒绝，防止副作用重复调度。
- 当前断流异常的 `completed_items` 保持原生 dict 形态，这是 request 兼容路径所需；完整 response 返回标准 item，后续 Turn ledger 可直接消费。

## Review 修复

Important review 项已按 TDD 修复：

- `StreamInterrupted` 重新抛出前显式附加当前已完成 items；普通连接异常也继续转换为携带相同 items 的中断异常。
- `output_item.done` 以 provider item id/output index 去重；相同 completed 事件只接受一次，不会把重复工具带入 request 的中断交付路径。不同 item 复用 call id 会作为确定性 `ValueError` 暴露，不包装成可重试断流。
- `response.completed.output` 内部的重复 item id 不再静默丢弃，而是在接受 response 前拒绝；同一 item 在 done 与 completed 各出现一次仍正常合并。
- Assistant item callback 采用“累计正文状态变化才发送”的语义；done/completed 重带同一正文不重复回调。最终 phase/end_turn 始终可从 normalized response 读取，reasoning 不进入 Assistant callback。
- request 使用独立的 `normalized_response` 与 `legacy_response` 变量，旧 mapping 新增 `_pgagent_normalized_response` sidecar；Responses hosted/message、Chat unknown hints 和中断完成工具都可被后续 Turn ledger 直接读取。
- Chat Completions 到达合法 finish reason 后若 usage 尾流中断，保留已确认完成的标准 response，并记录 `_stream_tail_error` 用量元数据。

修复 RED（`backend` cwd）：

```text
E:\\anaconda3\\envs\\agent_dock\\python.exe -m pytest tests/test_model_protocol_output.py -q
```

关键输出：`3 failed, 3 passed`。失败分别为 duplicate done 被保留两次、final output 重复 id 未拒绝、Assistant done 回调重复同一正文状态；request sidecar 的变量覆盖由 review 静态复核确认。

修复 GREEN 与聚焦回归：

```text
E:\\anaconda3\\envs\\agent_dock\\python.exe -m pytest tests/test_model_protocol_output.py tests/test_model_gateway.py tests/test_model_responses.py tests/test_loop_guard.py -q
```

结果：`107 passed`。`compileall -q src/model` 和 `git diff --check` 通过。pytest 结束时仍有本机临时目录清理 `PermissionError`（所有测试已完成且为通过状态），与被测代码无关。

## Retry 累积复查修复

复查发现 request 在多次 Responses 中断时直接 `extend()` 已完成 items，同一 provider item 会跨重试累积，最终成功 response 构造标准模型时触发重复 ID。现改为每次中断都使用 `merge_replayed_items()` 合并先前与本次已完成 items；相同 provider item 只保留一次，续接请求、工具提前交付和最终 response 投影都使用去重后的集合。

RED：

```text
E:\\anaconda3\\envs\\agent_dock\\python.exe -m pytest tests/test_model_responses.py::test_responses_deduplicates_same_completed_item_across_repeated_interruptions -q
```

关键输出：`ValueError: duplicate item id: rs-1`，发生在第三次请求成功后的 normalized response 构造阶段。

GREEN：同一聚焦用例 `1 passed`；完整 Task 2 + loop guard 聚焦回归 `108 passed`。pytest 结束后仍仅有已记录的 Windows 临时目录清理警告。
