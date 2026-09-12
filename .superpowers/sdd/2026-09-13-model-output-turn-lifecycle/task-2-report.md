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
