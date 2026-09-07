# PGAgent Agent Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 PGAgent 增加本地结构化诊断日志和脱敏的运行诊断 UI，使每个模型步骤、工具调用和失败都能通过统一关联字段定位。

**Architecture:** 现有 `AgentRuntime`/`RunCoordinator` 继续拥有运行语义；新增 `observability` 模块负责上下文、脱敏和 JSONL 文件输出。所有持久事件通过一个事件写入服务分配同一 Run 内的顺序号，公共 API 与 SSE 复用同一安全投影，文件日志保留脱敏堆栈但不对浏览器开放。

**Tech Stack:** Python 3、FastAPI、SQLAlchemy、SQLite、标准库 `logging/contextvars`、React 19、TypeScript、Vitest、pytest

**Spec:** `docs/superpowers/specs/2026-09-07-agent-observability-design.md`

## Global Constraints

- 不接入远程日志服务或新增外部运行依赖。
- 不记录完整模型 prompt/response、工具结果、文件内容、补丁正文、stdout/stderr 或完整审批参数。
- 不开放读取本地 JSONL 日志的 HTTP API。
- 文件日志默认单片 20 MB、按天分片、保留 14 天；`RunEvent` 不自动清理。
- 不新增哈希、冻结 contract、baseline 或发布 gate；保留项目已有安全措施。
- 日志故障不能触发模型/工具重放，不能把失败伪装成成功。
- 只修改本计划列出的直接相关文件；保留工作树已有的其他修改。
- 新增中文注释只解释不直观的上下文传播、脱敏和事件顺序约束，不复述代码。

---

## File Map

### New backend files

- `backend/src/observability/__init__.py`：可观察性模块公共入口。
- `backend/src/observability/context.py`：运行关联上下文的绑定、读取和恢复。
- `backend/src/observability/redaction.py`：结构化字段和自由文本的脱敏/摘要。
- `backend/src/observability/logging.py`：JSON formatter、日期和大小轮转、过期清理、启动配置。
- `backend/src/persistence/run_events.py`：集中创建 `RunEvent`、分配同 Run 顺序号并返回公共写入对象。
- `backend/tests/test_observability.py`：上下文、脱敏、formatter、轮转与故障测试。

### Modified backend files

- `backend/src/config/settings.py`、`.env.example`：日志配置。
- `backend/src/main.py`：在生命周期业务动作前初始化日志。
- `backend/src/persistence/models.py`、`backend/src/persistence/database.py`：`RunEvent.trace_id/sequence` 与 SQLite 迁移。
- `backend/src/runs/lifecycle.py`、`backend/src/runs/runtime_factory.py`：Run 上下文、统一事件写入和运行失败日志。
- `backend/src/agent/engine.py`、`backend/src/model/retry.py`：模型步骤完成、重试和失败诊断。
- `backend/src/api/routes/shared.py`、`backend/src/api/routes/runs.py`、`backend/src/api/runtime.py`：公共事件投影、筛选分页和 SSE 投影。
- `backend/src/api/routes/approvals.py`、`backend/src/api/routes/sessions.py`、`backend/src/api/schemas.py`：审批、后台任务、协作/委派结果的安全响应。
- `backend/src/tasks/background.py`、`backend/src/agents/collaboration.py`：后台/协作日志只写关联字段和摘要。
- `backend/src/context/compaction.py`、`backend/src/runs/delegation.py`、`backend/src/sessions/delivery.py`：改用集中事件写入服务。
- `backend/tests/test_database.py`、`backend/tests/test_runtime_api.py`、`backend/tests/test_run_stream.py`、`backend/tests/test_loop_guard.py`、`backend/tests/test_background_jobs.py`、`backend/tests/test_team_collaboration.py`：事件、SSE 和敏感信息回归。

### Modified frontend files

- `frontend/src/types.ts`、`frontend/src/api.ts`：诊断事件和分页查询类型。
- `frontend/src/features/runs/RunsPage.tsx`：诊断筛选、游标加载和关联信息。
- `frontend/src/runEventPresentation.ts`：新诊断事件的安全展示。
- `frontend/src/features/sessions/presentation.tsx`、`frontend/src/thoughtTimeline.ts`：禁止回退到原始参数/错误正文。
- `frontend/src/styles/catalog-usage.css` 或当前 Runs 页面所属样式文件：诊断控件与分页布局。
- `frontend/src/runEventPresentation.test.ts`、`frontend/src/api.test.ts`、新增 `frontend/src/features/runs/RunsPage.test.tsx`：展示与交互测试。

---

### Task 1: Observability Context and Redaction Primitives

**Files:**
- Create: `backend/src/observability/__init__.py`
- Create: `backend/src/observability/context.py`
- Create: `backend/src/observability/redaction.py`
- Create: `backend/tests/test_observability.py`

**Interfaces:**
- Produces: `bind_observability_context(**fields) -> ContextManager[None]`
- Produces: `current_observability_context() -> dict[str, JSONScalar]`
- Produces: `redact_mapping(value: Mapping[str, Any], *, max_text_chars: int = 2000) -> dict[str, Any]`
- Produces: `redact_text(value: str, *, limit: int = 2000) -> str`
- Produces: `summarize_command(command: Sequence[str] | str, *, cwd: str | None, workspace_root: str | None) -> dict[str, Any]`
- Consumes: no application service; standard library only.

- [ ] **Step 1: Write failing context propagation tests**

Add tests proving nested binding restores the outer values and `asyncio.create_task()` inherits the current context:

```python
async def test_observability_context_propagates_and_restores() -> None:
    assert current_observability_context() == {}
    with bind_observability_context(trace_id="trace-1", run_id="run-1", step=2):
        inherited = await asyncio.create_task(_read_context())
        assert inherited == {"trace_id": "trace-1", "run_id": "run-1", "step": 2}
        with bind_observability_context(tool_call_id="call-1"):
            assert current_observability_context()["tool_call_id"] == "call-1"
        assert "tool_call_id" not in current_observability_context()
    assert current_observability_context() == {}
```

- [ ] **Step 2: Write failing redaction tests**

Cover nested mappings/lists, case-insensitive secret field names, Bearer/JWT, URL query credentials, database URLs, `KEY=value`, NUL removal, truncation, and command summaries:

```python
def test_redaction_removes_secret_fields_and_text_patterns(tmp_path: Path) -> None:
    source = {
        "Authorization": "Bearer abc.def.ghi",
        "nested": {"api_key": "sk-secret", "safe": "visible"},
        "error": "request token=top-secret url=https://x.test/a?api_key=value",
    }
    redacted = redact_mapping(source)
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["nested"] == {"api_key": "[REDACTED]", "safe": "visible"}
    serialized = json.dumps(redacted)
    assert "top-secret" not in serialized
    assert "api_key=value" not in serialized
```

- [ ] **Step 3: Run the new tests and verify failure**

Run: `cd backend; pytest tests/test_observability.py -q`

Expected: collection fails because `src.observability` does not exist.

- [ ] **Step 4: Implement context binding**

Use one immutable dictionary stored in a `ContextVar`; only allow the documented correlation keys. `bind_observability_context` must merge fields and restore the previous token in `finally`.

```python
@contextmanager
def bind_observability_context(**fields: JSONScalar) -> Iterator[None]:
    clean = {key: value for key, value in fields.items() if key in CONTEXT_KEYS and value is not None}
    token = _CONTEXT.set({**_CONTEXT.get(), **clean})
    try:
        yield
    finally:
        _CONTEXT.reset(token)
```

- [ ] **Step 5: Implement redaction and safe summaries**

Keep redaction recursive and bounded. Secret-named fields become the literal `[REDACTED]`; arbitrary strings pass through pattern redaction and truncation. Command summaries contain only `executable`, `argument_count`, and relative `cwd`.

- [ ] **Step 6: Run focused tests**

Run: `cd backend; pytest tests/test_observability.py -q`

Expected: all Task 1 tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add backend/src/observability backend/tests/test_observability.py
git commit -m "feat: add observability context and redaction"
```

---

### Task 2: Structured JSONL Logging, Rotation, and Configuration

**Files:**
- Create: `backend/src/observability/logging.py`
- Modify: `backend/src/observability/__init__.py`
- Modify: `backend/src/config/settings.py`
- Modify: `.env.example`
- Modify: `backend/src/main.py`
- Test: `backend/tests/test_observability.py`
- Test: `backend/tests/test_main.py`

**Interfaces:**
- Consumes: `current_observability_context`, `redact_mapping`, `redact_text` from Task 1.
- Produces: `configure_observability_logging(*, level: str, log_dir: Path, retention_days: int, max_bytes: int) -> None`
- Produces: `JsonLineFormatter.format(record: LogRecord) -> str`
- Produces: `DailySizeRotatingFileHandler(log_dir: Path, *, max_bytes: int, retention_days: int)`
- Produces: `log_event(logger: logging.Logger, event_type: str, *, level: int = logging.INFO, **fields: Any) -> None`

- [ ] **Step 1: Add failing formatter and rotation tests**

Tests must parse every emitted line with `json.loads`, assert the fixed fields/correlation fields, verify exception text is redacted, force a tiny `max_bytes` rollover, move a matching file beyond 14 days, and confirm only matching expired PGAgent files are removed.

```python
def test_json_formatter_includes_context_without_secrets() -> None:
    with bind_observability_context(trace_id="trace-1", run_id="run-1", step=3):
        logger.error("provider failed token=hidden", extra={"event_type": "model_failed"})
    payload = json.loads(stream.getvalue())
    assert payload["trace_id"] == "trace-1"
    assert payload["run_id"] == "run-1"
    assert payload["step"] == 3
    assert "hidden" not in json.dumps(payload)
```

- [ ] **Step 2: Add failing settings/startup tests**

Assert defaults resolve to `data/logs`, `INFO`, `14`, and `20 * 1024 * 1024`; assert invalid non-positive retention/size values are rejected by settings validation. Patch `configure_observability_logging` and verify it is called before `init_db()` in lifespan startup.

- [ ] **Step 3: Run focused tests and verify failure**

Run: `cd backend; pytest tests/test_observability.py tests/test_main.py -q`

Expected: new logging/configuration assertions fail.

- [ ] **Step 4: Implement formatter and file handler**

Use standard library `logging`. The handler calculates the active filename from UTC date, checks size before emit, increments `.1`, `.2`, and cleans at most once per UTC day. Override `handleError` to write one bounded diagnostic to stderr without raising into Agent code.

- [ ] **Step 5: Wire settings and startup**

Add typed settings:

```python
log_level: str = "INFO"
log_dir: str | None = None
log_retention_days: int = Field(default=14, ge=1)
log_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1)

@property
def resolved_log_dir(self) -> Path:
    return Path(self.log_dir).expanduser().resolve() if self.log_dir else self.data_dir / "logs"
```

Call `configure_observability_logging(...)` as the first statement in `lifespan`, before directory setup and database work. Keep console logging enabled and honor `PGAGENT_LOG_LEVEL`.

- [ ] **Step 6: Document environment values**

Add exact defaults to `.env.example`; do not add additional flags.

- [ ] **Step 7: Run focused tests**

Run: `cd backend; pytest tests/test_observability.py tests/test_main.py -q`

Expected: all focused tests pass and temporary log files contain valid JSONL.

- [ ] **Step 8: Commit Task 2**

```bash
git add .env.example backend/src/observability backend/src/config/settings.py backend/src/main.py backend/tests/test_observability.py backend/tests/test_main.py
git commit -m "feat: configure rotating diagnostic logs"
```

---

### Task 3: Ordered RunEvent Persistence and Trace Correlation

**Files:**
- Create: `backend/src/persistence/run_events.py`
- Modify: `backend/src/persistence/models.py`
- Modify: `backend/src/persistence/database.py`
- Modify: `backend/src/api/schemas.py`
- Modify: every direct `RunEvent(...)` call site in `backend/src/api/runtime.py`, `backend/src/context/compaction.py`, `backend/src/runs/delegation.py`, `backend/src/runs/lifecycle.py`, `backend/src/sessions/delivery.py`, and `backend/src/api/routes/runs.py`
- Test: `backend/tests/test_database.py`
- Test: `backend/tests/test_runtime_api.py`

**Interfaces:**
- Produces: `append_run_event(db: OrmSession, *, run_id: str, event_type: str, payload: Mapping[str, Any] | None = None, step: int | None = None, trace_id: str | None = None) -> RunEvent`
- Produces: `RunEvent.trace_id: str | None`
- Produces: `RunEvent.sequence: int`
- Consumes: current observability context from Task 1 when `trace_id` is omitted.

- [ ] **Step 1: Write failing schema and migration tests**

Create an old-form SQLite database without the new columns, run `init_db()`, and assert existing rows receive deterministic sequence values ordered by `created_at, id`. Assert `RunEventRead` includes `trace_id` and `sequence`.

- [ ] **Step 2: Write failing ordered append tests**

Append events from multiple threads for one Run and assert sequences are exactly `1..N`, with no duplicate. Append to a second Run and assert it starts at `1`.

```python
assert [event.sequence for event in first_run_events] == list(range(1, count + 1))
assert second_run_event.sequence == 1
```

- [ ] **Step 3: Run focused tests and verify failure**

Run: `cd backend; pytest tests/test_database.py tests/test_runtime_api.py -q`

Expected: missing fields/helper and ordering assertions fail.

- [ ] **Step 4: Add columns and migration**

Add nullable `trace_id` and non-null integer `sequence`. Backfill existing rows per Run using stable `created_at, id` order before enforcing non-null behavior in application writes. Add an index on `(run_id, sequence)` for ordered retrieval; do not add a new cross-system gate.

- [ ] **Step 5: Implement centralized append**

Because this application is a single-process local SQLite service, serialize per-Run sequence allocation with a process-local keyed lock, query the current maximum inside the caller transaction, and add the event without committing it. The caller remains transaction owner. Explain this single-process constraint in a Chinese comment and test the actual concurrent call pattern.

- [ ] **Step 6: Replace all direct event constructors**

Use `rg -n "RunEvent\\(" backend/src` and replace every application write with `append_run_event`. ORM class declaration/imports are the only expected remaining matches. Preserve each caller's existing commit boundary and private snapshot payload.

- [ ] **Step 7: Update snapshot ordering**

Queries for latest `runtime_snapshot` and delegation links must order by `sequence.desc()` with `id.desc()` only as a legacy fallback.

- [ ] **Step 8: Run focused tests and search invariants**

Run:

```text
cd backend
pytest tests/test_database.py tests/test_runtime_api.py -q
rg -n "RunEvent\(" src
```

Expected: tests pass; the search shows only the model declaration and the centralized writer.

- [ ] **Step 9: Commit Task 3**

```bash
git add backend/src/persistence backend/src/api backend/src/context/compaction.py backend/src/runs backend/src/sessions/delivery.py backend/tests/test_database.py backend/tests/test_runtime_api.py
git commit -m "feat: order and correlate run events"
```

---

### Task 4: Instrument the Runtime, Model, Tool, Background, and Delegation Boundaries

**Files:**
- Modify: `backend/src/runs/lifecycle.py`
- Modify: `backend/src/runs/runtime_factory.py`
- Modify: `backend/src/agent/engine.py`
- Modify: `backend/src/model/retry.py`
- Modify: `backend/src/tools/registry.py`
- Modify: `backend/src/tasks/background.py`
- Modify: `backend/src/agents/collaboration.py`
- Modify: `backend/src/mcp/runtime.py`
- Modify: `backend/src/mcp/integration.py`
- Modify: `backend/src/memory/service.py`
- Modify: `backend/src/memory/pipeline.py`
- Test: `backend/tests/test_loop_guard.py`
- Test: `backend/tests/test_tool_runtime.py`
- Test: `backend/tests/test_background_jobs.py`
- Test: `backend/tests/test_task_delegation.py`
- Test: `backend/tests/test_team_collaboration.py`
- Test: `backend/tests/test_mcp_runtime.py`
- Test: `backend/tests/test_memory_pipeline.py`

**Interfaces:**
- Consumes: `bind_observability_context`, `log_event`, `append_run_event`.
- Produces: event payloads consistently carrying `trace_id`, `run_id`, `step`, `tool_call_id` where applicable.
- Produces: `model_step_finished` for successful model calls with duration/usage but no response content.

- [ ] **Step 1: Add failing model lifecycle tests**

For success, retry-success, timeout and terminal provider failure, capture durable events and logs. Assert each `model_step_started` has a matching `model_step_finished` or `model_failed`, the same step, duration, retry metadata, and no prompt/response/API key.

- [ ] **Step 2: Add failing tool lifecycle tests**

Cover sequential and parallel tool calls, approval-required calls and tool exceptions. Assert start/finish correlation by `tool_call_id`, safe argument metadata, duration, `ok/changed/error_code`, and no result body.

- [ ] **Step 3: Add failing background/delegation/MCP/memory tests**

Assert background jobs and children inherit the parent trace, record their own IDs, and log only command/output lengths and stable statuses. Assert MCP headers, environment values, collaboration payload bodies and memory source text never appear in JSONL or public events.

- [ ] **Step 4: Run focused tests and verify failure**

Run:

```text
cd backend
pytest tests/test_loop_guard.py tests/test_tool_runtime.py tests/test_background_jobs.py tests/test_task_delegation.py tests/test_team_collaboration.py tests/test_mcp_runtime.py tests/test_memory_pipeline.py -q
```

Expected: missing finished events/context/log assertions fail.

- [ ] **Step 5: Bind Run context in coordinator entrypoints**

In `_execute`, `_resume`, and delegated/background continuation entrypoints, load the Run, Turn trace, and parent Run before runtime work, then wrap the full operation:

```python
with bind_observability_context(
    trace_id=trace_id,
    run_id=run.id,
    turn_id=run.turn_id,
    parent_run_id=parent_run_id,
):
    await execute_runtime()
```

Use explicit binding inside background worker threads rather than assuming automatic `contextvars` propagation.

- [ ] **Step 6: Add model and tool diagnostic events**

Publish `model_step_finished` immediately after a successful provider turn is normalized. Reuse the existing `tool_started/tool_finished` sites and `safe_tool_argument_summary`; add file log calls at these boundaries. Never log raw `messages`, `response`, `ToolResult.content`, arguments or provider headers.

- [ ] **Step 7: Add exception logging at ownership boundaries**

Before existing state transitions, add `logger.exception` or `log_event(..., level=ERROR)` to coordinator integration failures, model terminal failures, tool wrapper exceptions, background worker failures, MCP startup/call failures, memory job failures and child Run failures. Preserve the existing error propagation and terminal delivery.

- [ ] **Step 8: Run focused tests**

Run the Task 4 command again.

Expected: all focused tests pass; captured diagnostic lines contain correlation fields and no forbidden bodies.

- [ ] **Step 9: Commit Task 4**

```bash
git add backend/src/agent backend/src/model backend/src/tools backend/src/runs backend/src/tasks backend/src/agents backend/src/mcp backend/src/memory backend/tests
git commit -m "feat: trace agent runtime steps and failures"
```

---

### Task 5: One Safe Projection for Run Events, SSE, Approvals, and Task APIs

**Files:**
- Modify: `backend/src/api/routes/shared.py`
- Modify: `backend/src/api/routes/runs.py`
- Modify: `backend/src/api/runtime.py`
- Modify: `backend/src/api/routes/approvals.py`
- Modify: `backend/src/api/routes/sessions.py`
- Modify: `backend/src/api/schemas.py`
- Modify: `backend/src/runs/stream.py`
- Test: `backend/tests/test_database.py`
- Test: `backend/tests/test_runtime_api.py`
- Test: `backend/tests/test_run_stream.py`
- Test: `backend/tests/test_background_jobs.py`
- Test: `backend/tests/test_team_collaboration.py`

**Interfaces:**
- Produces: `public_run_event(event: RunEvent | Mapping[str, Any]) -> RunEventRead | None`
- Produces: `RunEventPage(items: list[RunEventRead], next_before: int | None)`
- Produces: GET query parameters `event_type: list[str] | None`, `step: int | None`, `errors_only: bool`, `before: int | None`, `limit: int = 100` with `1 <= limit <= 200`.
- Produces: safe approval/background/collaboration/delegation response schemas containing summaries instead of raw bodies.

- [ ] **Step 1: Add malicious payload black-box tests**

Persist events containing unknown keys, `internal_error`, raw arguments, Bearer tokens, stdout/stderr, model messages and snapshot data. Fetch through GET, POST response and SSE replay/live paths. Assert the forbidden sentinel is absent from serialized HTTP/SSE bytes.

```python
for response_bytes in (get_response.content, post_response.content, sse_bytes):
    assert b"SUPER_SECRET_SENTINEL" not in response_bytes
```

- [ ] **Step 2: Add approval/background/collaboration response tests**

Create raw stored arguments/output/error values required by runtime recovery, then assert public APIs return only keys/types/lengths, IDs, statuses, exit code and stable public error code. Confirm storage remains sufficient for existing resume behavior.

- [ ] **Step 3: Add filtering and cursor tests**

Create mixed ordered events and assert event type, step, errors-only and `before` filters compose correctly. Fetch pages of two and assert no duplicate/missing sequence across cursors.

- [ ] **Step 4: Run focused tests and verify failure**

Run:

```text
cd backend
pytest tests/test_database.py tests/test_runtime_api.py tests/test_run_stream.py tests/test_background_jobs.py tests/test_team_collaboration.py -q
```

Expected: SSE/POST/secondary API leak tests and pagination tests fail before implementation.

- [ ] **Step 5: Make public projection reusable**

Promote the existing private projection helpers in `shared.py` to the single API-boundary implementation. Keep runtime snapshot/checkpoint event types completely hidden. Add `trace_id` and `sequence` as top-level safe fields; keep payload on an explicit whitelist.

- [ ] **Step 6: Apply projection to SSE and POST**

Before broker publication, project durable events to the same public shape. Transient token/thought events keep their existing explicit whitelist and never accept arbitrary event payload. `POST /events` persists through `append_run_event` and returns the projected response or rejects a private event type.

- [ ] **Step 7: Implement filtering and cursor page**

Filter in SQL before projection, order descending for page retrieval, then return display items in chronological order. Use `sequence` as the cursor. Do not add a separate count query.

- [ ] **Step 8: Replace raw secondary API responses**

Approval responses expose `argument_summary`; background job responses expose command summary, `output_chars`, status and exit code; collaboration/delegation responses expose IDs, status and safe result/error summaries. Do not delete raw database fields needed by execution/recovery.

- [ ] **Step 9: Run focused tests**

Run the Task 5 command again.

Expected: all leak, pagination, SSE and existing recovery tests pass.

- [ ] **Step 10: Commit Task 5**

```bash
git add backend/src/api backend/src/runs/stream.py backend/tests
git commit -m "feat: expose safe paged run diagnostics"
```

---

### Task 6: Runs Diagnostic UI

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/features/runs/RunsPage.tsx`
- Modify: `frontend/src/runEventPresentation.ts`
- Modify: `frontend/src/features/sessions/presentation.tsx`
- Modify: `frontend/src/thoughtTimeline.ts`
- Modify: the existing Runs/timeline rules in `frontend/src/styles/catalog-usage.css` or the stylesheet confirmed by import tracing
- Modify: `frontend/src/api.test.ts`
- Modify: `frontend/src/runEventPresentation.test.ts`
- Create: `frontend/src/features/runs/RunsPage.test.tsx`

**Interfaces:**
- Consumes: `RunEventPage`, `RunEvent.sequence`, `trace_id`, safe payload fields from Task 5.
- Produces: `RunEventFilters = { eventType?: string; step?: number; errorsOnly?: boolean; before?: number; limit?: number }`
- Produces: `api.listRunEvents(runId: string, filters: RunEventFilters) -> Promise<RunEventPage>`

- [ ] **Step 1: Write failing API query serialization tests**

Assert omitted filters create no empty query keys, repeated/selected event filters are encoded correctly, and cursor/limit are numeric.

- [ ] **Step 2: Write failing RunsPage interaction tests**

Mock the events endpoint and test:

- opening a Run detail;
- selecting an event type;
- selecting a step;
- toggling errors-only;
- loading the next cursor page without duplicates;
- rendering duration/retry/error code and child/background links;
- empty/loading/error states.

- [ ] **Step 3: Write failing no-raw-fallback tests**

Pass events/approvals with `content`, `arguments`, `stdout`, `stderr`, `internal_error` and raw exception fields. Assert none appear in the DOM when safe summaries are absent.

- [ ] **Step 4: Run focused frontend tests and verify failure**

Run:

```text
cd frontend
npm test -- --run src/api.test.ts src/runEventPresentation.test.ts src/features/runs/RunsPage.test.tsx src/thoughtTimeline.test.ts
```

Expected: missing paged API and diagnostics UI assertions fail.

- [ ] **Step 5: Implement typed API and diagnostic state**

Add the exact page/filter interfaces. In `RunDetails`, keep filters local to the selected Run, reset cursor/items when a filter changes, and append unique events by `sequence` when loading more.

- [ ] **Step 6: Implement final-reader UI**

Use user-facing labels only: “全部事件”, “仅看错误”, “步骤”, “加载更多”. Show time, event title, safe detail, duration, retry, error code and links. Do not include implementation notes, logging architecture, file paths or developer instructions in the page body.

- [ ] **Step 7: Remove raw fallbacks**

Update presentation helpers so missing safe summaries result in omitted detail, not a fallback to raw payload. Preserve existing useful safe timeline content and accessibility labels.

- [ ] **Step 8: Run focused tests and build**

Run:

```text
cd frontend
npm test -- --run src/api.test.ts src/runEventPresentation.test.ts src/features/runs/RunsPage.test.tsx src/thoughtTimeline.test.ts
npm run build
```

Expected: tests and production build pass.

- [ ] **Step 9: Commit Task 6**

```bash
git add frontend/src
git commit -m "feat: add run diagnostics view"
```

---

### Task 7: End-to-End Regression and Real Local Run Validation

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/MODULE_LAYOUT.md`
- Test: all backend and frontend tests.

**Interfaces:**
- Consumes: complete implementation from Tasks 1-6.
- Produces: documented operator path `data/logs/*.jsonl` and verified correlation evidence.

- [ ] **Step 1: Update architecture documentation**

Document the two layers, ownership of `observability`, safe public event boundary, correlation fields, file defaults and the fact that file logs are local-only. Keep the docs reader-facing; do not include implementation diary or review notes.

- [ ] **Step 2: Run the complete backend suite**

Run: `cd backend; pytest -q`

Expected: full suite passes.

- [ ] **Step 3: Run the complete frontend suite and build**

Run:

```text
cd frontend
npm test -- --run
npm run build
```

Expected: all tests and build pass.

- [ ] **Step 4: Run static diff validation**

Run from repository root:

```text
git diff --check
rg -n "RunEvent\(" backend/src
rg -n "SUPER_SECRET_SENTINEL" data/logs backend frontend
```

Expected: no whitespace errors; RunEvent writes remain centralized; no test sentinel appears in application output or retained logs.

- [ ] **Step 5: Execute a real local Run**

Start PGAgent through the repository's existing launcher and use a temporary test Session. Perform:

1. one successful model step;
2. one safe read-only tool call;
3. one controlled tool or provider failure that does not alter user data.

Capture the Run ID and query `/api/runs/{run_id}/events`.

- [ ] **Step 6: Reconcile the three observable layers**

Verify the same `trace_id`, `run_id`, step and tool call ID appear where applicable in:

- SQLite `run_events`;
- the safe API/UI diagnostic timeline;
- `data/logs/pgagent-*.jsonl`.

Verify the local JSON line contains the controlled exception stack, while API/UI contain only safe error fields. Confirm no prompt, response, file body, stdout/stderr, credential or approval arguments appear in either output surface.

- [ ] **Step 7: Run independent code review**

Request a code review focused on correctness, lost events, log recursion/I/O failure, sequence concurrency, secret leakage, SSE/history consistency and preservation of recovery semantics. Fix only reproducible in-scope findings, then rerun the affected focused tests and complete suites.

- [ ] **Step 8: Commit final documentation or review fixes**

```bash
git add docs/ARCHITECTURE.md docs/MODULE_LAYOUT.md
git commit -m "docs: document agent observability"
```

If review exposes a reproducible in-scope defect, return to the owning Task, change only that Task's listed files, rerun its focused verification, and create a separate fix commit with those exact paths before continuing this checklist.

- [ ] **Step 9: Final repository check**

Run:

```text
git status --short
git log -7 --oneline
```

Expected: only user-owned pre-existing changes remain unstaged; all observability commits are present locally and nothing is pushed unless explicitly requested.
