# PGAgent Model Output and Turn Lifecycle Implementation Plan

> **执行方式：** 使用测试驱动与子代理逐任务执行；每个任务先确认失败测试，再实施最小修改、运行聚焦测试并审查。任务完成后进行全量验证、历史会话真实对话测试和独立代码审查。

**Goal:** 将 PGAgent 从扁平 `ModelTurn + 默认候选验收` 重构为有序标准化输出项、response ledger、工具 drain 和持久 Turn 终态，消除流式正文迁移/消失并明确完成边界。

**Architecture:** Provider adapter 负责把 Responses/Chat Completions 映射为标准 output items；Agent Turn ledger 负责响应、工具与 follow-up 状态；Run 生命周期负责持久化与终态交付；前端按有序 Assistant items 渲染，只有 Turn 终态事件触发原子 hydration。

**Tech Stack:** Python 3、FastAPI、SQLAlchemy、LiteLLM/OpenAI 协议适配、React 19、TypeScript、Vitest、pytest

**Spec:** `docs/superpowers/specs/2026-09-13-model-output-turn-lifecycle-design.md`

## 全局约束

- 保留所有现有权限、审批、沙箱、LoopGuard、超时、Token、停止竞态和终态消息唯一性措施。
- 不新增数据库表/列；账本存入可选 runtime snapshot JSON。
- 不根据文本内容或 Chat Completions 的工具情况推断 phase。
- 不再默认安装 completion verifier，不产生新的 `acceptance_failed`；历史数据继续可读。
- 新正文事件不长期双发旧 `assistant_delta/thought_delta`；仅保留历史读取兼容。
- 新增中文注释只解释非直观状态边界和竞态约束。
- 保留工作区已有未跟踪日志和无关修改。

## Task 1：标准化模型输出数据结构

**Files:**

- Create: `backend/src/model/output.py`
- Modify: `backend/src/model/__init__.py`
- Test: `backend/tests/test_model_output.py`

- [ ] 先写测试，覆盖四类 item、phase/end_turn unknown、顺序、ID/call ID 重复拒绝和 response 完成状态。
- [ ] 运行聚焦测试并确认因类型未实现而失败。
- [ ] 实现最小 dataclass/enum/校验与序列化辅助，不创建外部冻结 contract。
- [ ] 运行聚焦测试、类型/导入检查并审查数据模型是否能同时表达 Responses 与 Chat Completions。

## Task 2：Responses 与 Chat Completions 协议适配

**Files:**

- Modify: `backend/src/model/protocols/responses.py`
- Modify: `backend/src/model/protocols/chat_completions.py`
- Modify: `backend/src/model/request.py`
- Test: `backend/tests/test_model_responses.py`
- Test: `backend/tests/test_model_gateway.py`
- Test: `backend/tests/test_model_protocol_output.py`

- [ ] 先写 Responses 测试：item delta/done、`phase/end_turn`、hosted/local 分离、completed 重复 item 去重、流中断不完成工具项。
- [ ] 先写 Chat Completions 测试：content/tool fragments 聚合、response 结束后产出工具 item、phase/end_turn 恒为 unknown。
- [ ] 运行聚焦测试并确认失败原因对应缺失的新接口。
- [ ] 让两个 adapter 输出 `NormalizedModelResponse`，同时保留协议续接所需 payload。
- [ ] 用 Assistant item 回调替代正文的 thought/answer 二选一通道；provider reasoning 保持私有续接语义。
- [ ] 运行聚焦测试并审查重复事件与中断边界。

## Task 3：Response ledger 与 Turn 状态机

**Files:**

- Create: `backend/src/agent/turn.py`
- Modify: `backend/src/agent/state.py`
- Modify: `backend/src/agent/loop.py`
- Modify: `backend/src/agent/engine.py`
- Test: `backend/tests/test_turn_completion.py`
- Test: `backend/tests/test_loop_guard.py`

- [ ] 先写状态机测试：纯文本终态、commentary/final 不决定完成、`end_turn=false` 触发 follow-up、local tool drain、hosted/local 混合和重复副作用调用拒绝。
- [ ] 运行聚焦测试并确认失败。
- [ ] 实现 response/item ledger 和结构化 follow-up decision。
- [ ] 将工具调度绑定到已完成 `LocalToolCallItem`；完成 response 后等待所有 local results commit。
- [ ] 从 `ModelTurn` 候选分支迁移 engine 主循环，确保工具开始不清空或迁移 Assistant 文本。
- [ ] 保留 `empty_model_output`、LoopGuard、超时、Token、审批和安全路由。
- [ ] 运行聚焦测试并审查 hosted/local 计数、重复调用和无 follow-up 的终态路径。

## Task 4：验收边界、审批、后台与恢复迁移

**Files:**

- Modify: `backend/src/agent/completion.py`
- Modify: `backend/src/runs/lifecycle.py`
- Modify: `backend/src/runs/continuation.py`
- Modify: `backend/src/runs/runtime_factory.py`
- Modify: `backend/src/runs/configuration.py`
- Modify: `backend/src/runs/delegation_format.py`
- Modify: `backend/src/context/compaction.py`
- Modify: `backend/src/config/settings.py`
- Test: `backend/tests/test_completion_acceptance.py`
- Test: `backend/tests/test_run_service.py`
- Test: `backend/tests/test_background_jobs.py`
- Test: `backend/tests/test_task_delegation.py`
- Test: `backend/tests/test_run_recovery.py`

- [ ] 先把测试改为新边界：默认无 verifier、后台/审批由 Turn 状态阻止完成、旧 acceptance 字段可读取、旧快照无 ledger 可恢复。
- [ ] 运行聚焦测试并确认旧默认 verifier 断言失败。
- [ ] 移除默认 `CompletionDecision` 热路径、候选重放、拒绝反馈/重采样和新 `acceptance_failed` 生产。
- [ ] 将原 verifier 中必要的后台/审批条件迁移到 Turn transition，把调用/结果一致性迁移到 result commit/drain。
- [ ] runtime snapshot 增加可选 output ledger；保持旧 verification/acceptance 字段反序列化。
- [ ] 审批恢复继续执行精确保存的 call，停止/拒绝后的晚结果不得改变终态。
- [ ] 运行聚焦恢复、审批、后台与 compaction 测试并审查兼容性。

## Task 5：持久 RunEvent、SSE 与终态交付顺序

**Files:**

- Modify: `backend/src/runs/lifecycle.py`
- Modify: `backend/src/sessions/delivery.py`
- Modify: `backend/src/api/routes/shared.py`
- Modify: `backend/src/api/runtime.py`
- Modify: `backend/src/runs/stream.py`
- Test: `backend/tests/test_run_service.py`
- Test: `backend/tests/test_runtime_api.py`
- Test: `backend/tests/test_run_stream.py`
- Test: `backend/tests/test_database.py`

- [ ] 先写事件顺序测试：Assistant item completed 可重放、delta 仅 transient、`model_response_completed` 非终态、`turn_completed` 在终态消息持久化后可见。
- [ ] 写持久化/SSE 竞态测试：收到 `turn_completed` 后立即查询消息必须得到唯一 `terminal_for_turn_id` 回复。
- [ ] 运行聚焦测试并确认失败。
- [ ] 发布新 item/response/Turn 事件并加入公共安全投影；工具事件增加安全关联 ID。
- [ ] 调整生命周期，先 `persist_terminal_response()`，成功后再发布 Turn 终态；旧 Run 状态仍正确落库。
- [ ] 保留旧事件读取和进程内 replay 行为，不把 SSE event_id 当持久序号。
- [ ] 运行聚焦事件、API、终态幂等和恢复测试。

## Task 6：前端有序 Assistant items 与原子 hydration

**Files:**

- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/sessionStream.ts`
- Modify: `frontend/src/features/sessions/sessionState.ts`
- Modify: `frontend/src/features/sessions/hooks/useRunTransport.ts`
- Modify: `frontend/src/features/sessions/presentation.tsx`
- Modify: `frontend/src/features/sessions/SessionsPage.tsx`
- Modify: `frontend/src/thoughtTimeline.ts`
- Modify: `frontend/src/styles/pgagent-ui.css`
- Test: `frontend/src/sessionStream.test.ts`
- Test: `frontend/src/features/sessions/presentation.test.tsx`
- Test: `frontend/src/thoughtTimeline.test.ts`
- Test: `frontend/src/features/sessions/hooks/useRunTransport.test.tsx`

- [ ] 先写 reducer/呈现测试：按 response/item/index 排序、Markdown 增量、工具事件后正文不变、response completed 不清理、Turn terminal 后等待消息 hydration 再原子替换。
- [ ] 写 legacy 事件重放测试，确保历史会话仍可显示。
- [ ] 运行前端聚焦测试并确认失败。
- [ ] 用 `assistantItems` 替换单一 `liveRun.draft`，统一 commentary/final/unknown 的 `MarkdownContent` 渲染。
- [ ] reasoning/tools 继续进入执行详情，补齐新命名 SSE 事件监听。
- [ ] 以 `turn_completed/failed/stopped` 为新终态；兼容旧 run terminal，但不在 `model_response_completed` 时清理。
- [ ] 在持久终态消息出现前保留 live items，出现后原子移除，避免闪烁、重排和双份正文。
- [ ] 运行聚焦 Vitest、React 性能/可访问性检查并用实际页面验证。

## Task 7：风险专项测试与直接修复

**Files:**

- Test/modify only files directly involved in failures from Tasks 1-6.

- [ ] 重复 Responses item：验证副作用工具只执行一次。
- [ ] completed hosted item 后流中断：验证不会伪造本地结果或提前结束 Turn。
- [ ] 审批拒绝/用户停止与晚工具结果：验证终态不可逆且没有 follow-up。
- [ ] terminal persistence/SSE 竞态：验证事件之后消息一定可读。
- [ ] hosted/local 混合：验证 drain 只统计 local。
- [ ] 旧 snapshot：验证读取、继续或明确失败路径。
- [ ] streaming Markdown → tool → final：验证 DOM 内容稳定且 hydration 无重复。
- [ ] Chat Completions unknown phase 与空输出：验证不猜 phase、不重新采样空结果。
- [ ] 对每个真实失败只做直接相关最小修复，重复运行对应测试。

## Task 8：全量验证、历史会话真实对话与独立审查

**Files:**

- Modify only files needed to fix directly reproduced defects.
- Update: `docs/ARCHITECTURE.md`

- [ ] 更新架构文档，移除“所有候选必须通过默认验收”的过时描述，说明 output item、response、tool drain、Turn delivery 边界。
- [ ] Backend：运行全量 pytest。
- [ ] Frontend：运行全量 Vitest、lint 和 build。
- [ ] 运行 `git diff --check`，检查未跟踪日志未被纳入。
- [ ] 从本地数据库选取历史会话结构，复制到隔离测试会话；不得修改原历史会话。
- [ ] 使用当前真实 provider 配置完成纯文本、文本→工具→follow-up、Markdown、多工具以及可安全执行的审批/停止对话。
- [ ] 同时检查数据库终态消息、RunEvent 顺序、SSE/前端实际页面；记录模型/provider/会话/Run 标识和可复现结果。
- [ ] 对真实测试发现的问题直接修复并完成聚焦与全量回归。
- [ ] 使用独立 sol 审查完整 diff、状态机不变量、竞态、安全边界和测试证据；修复所有直接相关阻断问题并复审。
- [ ] 输出变更摘要、涉及路径、关键实现、验证结果和仍然存在的外部限制。
