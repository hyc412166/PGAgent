# Task 5 报告：持久 RunEvent、SSE 与终态交付顺序

## RED

- 场景：`turn_completed/turn_failed/turn_stopped` 应关闭 SSE，而 `model_response_completed` 不应关闭；终态消息应在 `turn_completed` 发布前可查询。
- 命令：`$env:PYTHONPATH='.'; pytest -q tests/test_run_stream.py::test_turn_terminal_events_close_stream_but_model_response_does_not tests/test_run_service.py::test_completed_outcome_publishes_turn_terminal_after_reply_is_delivered`（工作目录 `backend`）。
- 结果：测试收集阶段因当前环境缺少 `litellm`/`fastapi` 依赖失败，未能执行断言；该失败发生在实现前，属于环境阻断而非通过。

## GREEN

- 修改 `backend/src/runs/lifecycle.py`：assistant item completed 与 `model_response_completed` 按 RunEvent 顺序持久化；delta 只进入瞬时 broker；`persist_terminal_response()` 成功后在同一事务追加 `turn_completed/turn_failed/turn_stopped`，提交后按 assistant → model response → turn terminal 顺序发布；保留旧 `run_completed` 读取兼容并避免其提前关闭 SSE。
- 修改 `backend/src/runs/stream.py`：新增 `turn_*` 终态集合。
- 修改 `backend/src/api/routes/shared.py`：新增事件公开白名单、安全内容投影及工具 `response_id/item_id/call_id` 关联字段。
- 新增测试：`backend/tests/test_run_service.py`、`backend/tests/test_run_stream.py` 覆盖终态顺序、completed item 持久化和 delta 不落库。
- 命令：`python -m compileall -q backend/src backend/tests/test_run_service.py backend/tests/test_run_stream.py`。
- 结果：通过（退出码 0）。

补充验证（正确解释器 `E:\\anaconda3\\envs\\agent_dock\\python.exe`）：

- 命令：`python -m pytest -q tests/test_run_stream.py::test_turn_terminal_events_close_stream_but_model_response_does_not tests/test_run_service.py::test_completed_outcome_publishes_turn_terminal_after_reply_is_delivered tests/test_run_service.py::test_outcome_persists_completed_assistant_item_but_not_delta`。
- 结果：3 passed；pytest 退出时 Windows 临时目录清理产生既有 `PermissionError` 警告，不影响用例结果。

## 已知风险

- 当前机器缺少后端测试依赖，pytest 聚焦测试无法运行；需在完整依赖环境重新执行 Task 5 及 Task 3/4 回归。
- canonical `turn_*` 终态事件成为新 SSE 关闭信号；旧 `run_completed` 仍保留持久读取兼容，但不在终态交付路径中先行发布。
