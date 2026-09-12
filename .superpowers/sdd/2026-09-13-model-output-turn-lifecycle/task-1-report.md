# Task 1 报告：标准化模型输出数据结构

## 实现内容

- 在 `backend/src/model/output.py` 新增内部 dataclass 输出模型：`AssistantMessageItem`、`ReasoningItem`、`LocalToolCallItem`、`HostedToolItem` 和 `NormalizedModelResponse`。
- 新增 `OutputPhase`、`EndTurn`、`ResponseStatus` 枚举；缺失 `phase/end_turn` 时保持 `unknown`，不依据正文或工具调用推断。
- `NormalizedModelResponse` 将 items 按 `output_index` 排序，补齐缺失的本地顺序号，并在 response 接受前拒绝重复 `output_index`、item id 和 call id。
- 提供 `to_dict`、`as_dict`、`to_payload` 序列化辅助，同时保留 provider payload/reference、usage、finish reason 和错误状态等内部元数据；没有新增数据库或外部 API contract。
- 在 `backend/src/model/__init__.py` 导出上述类型。

## TDD 验证

### RED

命令（`backend` cwd）：

```text
E:\anaconda3\envs\agent_dock\python.exe -m pytest tests/test_model_output.py -q
```

关键输出：

```text
ImportError: cannot import name 'AssistantMessageItem' from 'src.model'
```

失败原因是标准化输出类型尚未实现，符合预期。

### GREEN

命令（`backend` cwd）：

```text
E:\anaconda3\envs\agent_dock\python.exe -m pytest tests/test_model_output.py -q --basetemp C:\Users\xxr\Desktop\agent_test\PGAgent\backend\.pytest-model-output
```

关键输出：

```text
........                                                                 [100%]
```

聚焦测试结果：8 passed。

兼容性聚焦验证：

```text
E:\anaconda3\envs\agent_dock\python.exe -m pytest tests/test_model_gateway.py tests/test_model_responses.py -q --basetemp C:\Users\xxr\Desktop\agent_test\PGAgent\backend\.pytest-model-output-integration
```

结果：48 passed。模型模块导入检查输出 `model-output-import-ok`；`compileall` 检查通过。

## 改动文件

- `backend/src/model/output.py`
- `backend/src/model/__init__.py`
- `backend/tests/test_model_output.py`

## 自审与风险

- 本次只实现内部标准数据结构，未修改 Responses/Chat Completions 适配器、工具调度、数据库或公开事件投影；后续任务仍需把两种协议映射到这些类型。
- duplicate 检查使用 response 作用域内的普通 ID 集合，不生成哈希、不增加持久化字段，也不把类型冻结为外部 contract。
- `phase` 仅规范化为 `commentary/final_answer/unknown`，`end_turn` 仅保留 provider 提示；response `completed` 只表示 provider response 成功关闭，不代表用户 Turn 已完成。
- 本机 `pytest` 默认解释器缺少 `litellm`，因此验证统一使用仓库已有的 `agent_dock` 解释器；该环境限制不影响上述结果。
