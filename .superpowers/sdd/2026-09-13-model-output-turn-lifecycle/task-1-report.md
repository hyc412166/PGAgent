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

## Review 修复：EndTurn 表示一致性

审查复现了原实现的问题：`EndTurn.UNKNOWN` 是继承 `str` 的枚举成员，默认 `bool(EndTurn.UNKNOWN)` 为 `True`；同时 `TRUE/FALSE` 被转换成普通 bool，导致 unknown 可能被通用真值判断误当成 true，且与 `EndTurn.TRUE/FALSE` 的身份比较不一致。

修复内容：

- `EndTurn` 现在覆盖 `__bool__`，只有 `EndTurn.TRUE` 为真，`FALSE/UNKNOWN` 均不会进入仅针对 true 的分支。
- 新增 `normalize_end_turn()`，将 bool、字符串、`None` 和枚举输入统一规范为 `EndTurn`，使字段内部始终使用单一类型；序列化仍输出 `true/false/unknown` 对应的 JSON 值。
- 测试新增 bool/string/None 输入、枚举身份与真值行为覆盖。

修复 TDD RED：

```text
E:\anaconda3\envs\agent_dock\python.exe -m pytest tests/test_model_output.py::test_end_turn_uses_one_enum_representation_and_unknown_is_not_truthy -q --basetemp C:\Users\xxr\Desktop\agent_test\PGAgent\backend\.pytest-end-turn-red
```

关键输出：5 个参数化用例均失败；显式 bool 输入仍为普通 `True/False`，且 `bool(EndTurn.UNKNOWN)` 为 `True`，符合回归复现预期。

修复 TDD GREEN：

```text
E:\anaconda3\envs\agent_dock\python.exe -m pytest tests/test_model_output.py::test_end_turn_uses_one_enum_representation_and_unknown_is_not_truthy -q --basetemp C:\Users\xxr\Desktop\agent_test\PGAgent\backend\.pytest-end-turn-green
```

关键输出：`..... [100%]`。

修复后回归：

```text
E:\anaconda3\envs\agent_dock\python.exe -m pytest tests/test_model_output.py tests/test_model_gateway.py tests/test_model_responses.py -q --basetemp C:\Users\xxr\Desktop\agent_test\PGAgent\backend\.pytest-model-output-fix
```

结果：61 passed；`compileall` 与显式 enum/真值检查均通过。

随后复核发现 `normalize_end_turn()` 未从 `src.model` 门面导出。已先用门面导入回归测试复现 `ImportError`，再在 `backend/src/model/__init__.py` 补齐导入与 `__all__`；修复后该参数化测试 5 passed，`compileall` 和门面 helper 检查通过。
