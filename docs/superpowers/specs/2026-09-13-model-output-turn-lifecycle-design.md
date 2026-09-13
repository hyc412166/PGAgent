# PGAgent 模型输出与 Turn 生命周期重构设计

## 1. 背景

PGAgent 当前把一次模型返回聚合成 `ModelTurn`，并在默认确定性验收开启时把候选正文先作为 `thought_delta` 推送。只有整个候选验收通过后，正文才重新以 `assistant_delta` 推送并最终持久化。这个流程混合了四种不同的“完成”：

- 单个模型输出项完成；
- 一次 provider response 完成；
- 本地工具调用完成并提交结果；
- 一次用户 Turn 已经得到可持久化的终态回复。

因此用户会先在执行详情里看到普通文本，验收后文本再移动到 Markdown 回复区域；若验收失败，整段已经可见的文本还会消失。Responses API 的 output item、Chat Completions 的 finish reason、本地工具、provider 托管工具和 Turn 交付也被同一个候选验收逻辑间接耦合。

## 2. 目标

1. 让 Assistant 文本从产生到完成始终位于 Assistant 消息区域，流式过程中不迁移、不重复播放、不因默认候选验收失败而整段消失。
2. 明确区分 output item、provider response、本地工具闭环和 Turn 终态。
3. 支持 Responses 原生 `phase/end_turn`，同时让 Chat Completions 在缺少该元数据时保持 `unknown`，不根据文本内容或是否调用工具猜测。
4. provider response 结束后先完成本地工具 drain，再决定继续调用模型还是交付 Turn。
5. 终态事件只在唯一终态 `ChatMessage` 成功持久化后发布。
6. 保留现有工具权限、参数校验、审批、沙箱、运行限额、停止竞态和终态消息幂等保护。
7. 历史快照、历史 RunEvent 和旧事件仍可读取与恢复；新运行只写新的输出项语义，不长期双写两套正文事件。

## 3. 非目标

- 不判断一段自然语言在语义上是否真正满足用户目标。
- 不为输出项新增数据库表或迁移现有 `ChatMessage` 一 Turn 一终态回复的模型。
- 不从 Chat Completions 文本、finish reason 或工具数量推断 `commentary/final_answer`。
- 不把 provider 托管工具伪装成本地 function tool。
- 不删除现有权限、安全、审批、LoopGuard、超时、Token、恢复和终态幂等措施。
- 不为本次重构增加哈希、冻结 contract、baseline 或发布 gate。

## 4. 总体流程

```text
Provider stream
  -> protocol adapter
  -> ordered normalized output items
  -> response/item ledger
  -> local tool scheduling and drain
  -> follow-up decision
  -> durable terminal reply
  -> Turn terminal event
```

`phase` 只决定消息如何呈现；`end_turn` 只表达 provider 对后续工作的提示。二者都不能单独证明 Turn 已完成。

## 5. 标准化输出模型

### 5.1 数据类型

`backend/src/model/output.py` 定义：

- `AssistantMessageItem`
  - `response_id`
  - `item_id`
  - `output_index`
  - `content`
  - `phase: commentary | final_answer | unknown`
  - `end_turn: true | false | unknown`
- `ReasoningItem`
  - 只保存协议续接或安全摘要需要的数据，不把私有 reasoning 作为普通可见正文。
- `LocalToolCallItem`
  - `call_id`
  - `tool_name`
  - 完整参数只进入现有安全执行路径；公开事件仅发送允许字段。
- `HostedToolItem`
  - 记录 provider 已执行的托管工具状态，不进入本地 `ToolRouter`，也不等待本地 `ToolResult`。
- `NormalizedModelResponse`
  - 有序 items；
  - provider response 的完成/失败状态；
  - 协议续接所需的 provider payload 或引用；
  - usage、finish reason 等元数据。

这些类型是内部运行模型，不冻结为外部长期 API contract。公开 SSE/RunEvent 继续经过现有安全投影。

### 5.2 ID 与顺序

- Responses 优先使用 provider 的 `response.id`、item id、`output_index` 和 `call_id`。
- Chat Completions 没有 item id 时，由 adapter 在一次 response 的作用域内生成顺序稳定的本地 ID；ID 只用于本次运行账本和事件关联，不作为跨运行业务标识。
- 同一 response 内的 `output_index` 必须稳定；重复的 item id 或 call id 在调度副作用前被拒绝。
- `response.completed` 可能重复携带已通过 `output_item.done` 收到的 items，adapter 必须合并而不能再次调度。

## 6. 协议适配

### 6.1 Responses API

- `response.output_item.done` 完成一个标准化 item。
- `response.output_text.delta` 追加到对应 `AssistantMessageItem`，立即发送 Assistant item delta。
- message item 中存在 `phase/end_turn` 时保留原值；缺失时为 `unknown`。
- `response.completed` 只关闭本次 provider response，并发送 `model_response_completed`；它不结束 Turn。
- 本地 `function_call` 和 hosted tool item 分开投影与记账。
- 流中断时保留已经完成的 items，但未形成确定完成边界的本地工具调用不得执行。

### 6.2 Chat Completions

- content、reasoning_content 和 tool call fragments 在 adapter 内按 choice/tool index 聚合。
- Assistant message 一律为 `phase=unknown`、`end_turn=unknown`。
- tool call 只有在参数拼接完成并到达 response 结束边界后才形成 `LocalToolCallItem`。
- `finish_reason` 只关闭 provider response，不直接结束 Turn。

## 7. Response ledger 与 Turn 状态机

`backend/src/agent/turn.py` 维护当前 Turn 的响应账本。最小状态信息包括：

- 已接受的 response；
- 每个 item 的完成状态和顺序；
- 本地工具调用的 `scheduled/running/result_committed` 状态；
- provider 托管工具的完成状态；
- 是否有待审批、后台等待、用户停止或必要 follow-up；
- 最近 Assistant item 的 `end_turn` 提示。

状态转换：

```text
acting
  -> draining_tools          当前 response 已关闭且存在本地工具
  -> awaiting_approval       工具进入现有审批边界
  -> observing               工具结果已全部提交，需要把结果发送给模型
  -> acting                  发起 follow-up provider response
  -> completed               可以交付终态回复
  -> stopped / failed        用户停止、审批拒绝或不可恢复失败
```

Turn 只有同时满足以下条件才可完成：

1. 当前 provider response 已成功关闭；
2. 所有已调度本地工具均有且仅有一个已提交结果；
3. 没有运行中工具、待审批或后台等待；
4. 不需要把新工具结果或观察结果发送给模型；
5. 没有显式 `end_turn=false`；
6. 没有用户停止或失败终态；
7. 存在可交付的 Assistant 文本；空成功输出继续转为 `empty_model_output`。

`phase=final_answer` 不等于 Turn 完成；`phase=commentary` 也不表示必须继续。Turn 状态机根据结构化运行事实决定，而不是根据消息文案判断。

## 8. 工具闭环与确定性检查边界

### 8.1 provider response 边界

- 流必须明确完成或失败；
- item 类型合法；
- item id、call id 不重复；
- 未完成的工具 fragment 不进入调度。

### 8.2 工具调用边界

- 工具已提供且启用；
- 参数可解析并满足 schema；
- 通过权限、策略、审批和沙箱限制；
- LoopGuard、超时、Token 和运行时限制继续生效。

### 8.3 工具结果提交边界

- 结果被规范化为现有 `ToolResult`；
- 结果工具名与所属 `ToolInvocation` 匹配；
- 每个 call id 只能提交一次；
- 审批拒绝或用户停止后到达的晚结果不能改变终态或继续模型循环。

### 8.4 response drain 边界

- 已调度本地调用数等于已提交结果数；
- 不存在 in-flight 本地工具；
- hosted tool 不参与本地调用/结果数量比较。

### 8.5 Turn 转移边界

- 无待审批、后台等待或未发送观察结果；
- 无停止/失败；
- 根据结构化状态决定 follow-up 或终态交付。

### 8.6 仅恢复/重放检查

- 旧快照中的重复、孤立调用或结果；
- 调用名与结果名不匹配；
- 快照损坏；
- 处于 running 且可能产生副作用的工具状态不明确时禁止自动重试。

默认热路径移除 `CompletionDecision`、候选缓冲/重放、验收报告、拒绝反馈与重新采样，以及默认 `completion_verifier` 安装。需要特殊任务验收时由显式 `TurnStopPolicy` 或上层工作流提供，默认不存在。

## 9. RunEvent、SSE 与终态持久化

新增公开事件：

- `assistant_message_started`
- `assistant_message_delta`
- `assistant_message_completed`
- `model_response_completed`
- `tool_started` / `tool_finished`，增加安全的 `response_id/item_id/call_id` 关联字段
- `turn_completed`
- `turn_failed`
- `turn_stopped`

约束：

- completed Assistant item 作为经过安全投影的 `RunEvent` 持久化，便于断流恢复；delta 继续只走 transient SSE，避免把每个字符写入数据库。
- `model_response_completed` 不是终态事件，前端收到后不关闭 SSE。
- `turn_completed` 必须在 `persist_terminal_response()` 成功并把 `ConversationTurn.reply_status` 更新为 delivered 后发布。
- 继续保留 `terminal_for_turn_id` 唯一约束，保证每个 Turn 至多一条可见终态回复。
- 旧 `run_completed` 只作为后端运行状态兼容事实，不能早于终态消息持久化触发新前端清理。
- 历史 `assistant_delta/thought_delta/run_completed` 和旧 acceptance 字段仍能读取；新运行不再把普通 Assistant 文本发送为 `thought_delta`。

## 10. 前端状态与呈现

`LiveRunState` 从单一 `draft` 改为有序 `assistantItems`。每个 item 保存：

- `responseId/itemId/outputIndex`
- `phase`
- `content`
- `status: streaming | completed`

显示规则：

- `commentary/final_answer/unknown` 全部使用同一个 `MarkdownContent`，在消息区域按输出顺序稳定呈现。
- reasoning 和工具活动继续进入可折叠执行详情。
- 工具开始/结束不得清空、移动或重建已经显示的 Assistant item。
- `model_response_completed` 只更新响应状态。
- 只有 `turn_completed/turn_failed/turn_stopped` 触发终态同步。
- live items 一直保留到 API 返回对应 `terminal_for_turn_id` 的持久化消息，然后原子替换，避免闪烁和双份正文。
- 历史旧事件通过兼容映射重建；新事件不再反向长期双发旧正文事件。

## 11. 恢复与兼容

- `runtime_snapshot` 可增加可选 `output_ledger`；旧快照没有该字段时走旧 transcript 恢复并构造最小账本。
- `verification_trace`、`acceptance_report`、`completion_verification_attempts` 等旧字段保持可反序列化，但新运行不再依赖或新增这些记录。
- 审批恢复必须使用快照中精确保存的 call id、工具名和参数，不重新让模型生成调用。
- 恢复遇到不明确的已运行副作用工具时进入可诊断失败/等待人工处理，不能自动重复执行。
- SSE `event_id` 仍是进程内重连标识；跨进程恢复依赖持久 RunEvent、Run 状态和会话消息，不把它当持久 sequence。

## 12. 主要风险与验证场景

1. Responses item 在 `output_item.done` 与 `response.completed` 中重复出现：同一副作用工具只能执行一次。
2. provider 托管工具完成后流中断：不得把 hosted item 当本地未闭环调用，也不得错误交付 Turn。
3. 审批拒绝/用户停止与晚到工具结果竞态：终态不能被晚结果覆盖，不能继续模型 follow-up。
4. 终态消息持久化与 SSE 竞态：`turn_completed` 之后立即读取消息必须已经得到唯一终态回复。
5. hosted/local 工具混合：只对 local call 做结果配对和 drain。
6. 旧快照恢复：无 `output_ledger` 仍可继续或明确失败，不因新字段缺失崩溃。
7. streaming Markdown → 工具 → final answer：正文始终在消息区域，工具事件不清空正文，持久化后无闪烁重复。
8. Chat Completions：始终 `phase=unknown`，不会根据 finish reason 或工具调用猜测 final。
9. 空成功输出：稳定返回 `empty_model_output`，不通过重新采样掩盖问题。

## 13. 交付判定

- 后端、前端相关单元与集成测试通过；
- 全量 pytest、Vitest、lint、build 和 diff 检查通过；
- 使用历史 PGAgent 会话构造真实模型对话测试，覆盖纯文本、文本后工具、审批/停止、混合工具和流式 Markdown；
- 检查实际持久化消息、RunEvent/SSE 顺序和 UI 行为；
- 对测试发现的直接相关缺陷完成修复和回归；
- 独立代码审查与最终风险复核无未解决的阻断问题。
