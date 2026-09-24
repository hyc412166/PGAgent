# Persistent Task Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move persistent task progress out of the conversation stream into a Codex-like branch/source popover attached to the child-agent detail panel.

**Architecture:** Reuse the existing `/tasks` and `/active-task` APIs and `DurableTask` data. A pure frontend ordering helper keeps the active task pinned while preserving creation order for history; a new source panel owns the task list and opens a reusable single-step task detail card.

**Tech Stack:** React 19, TypeScript, Vitest, existing CSS and lucide-react icons.

**Spec:** User-approved chat design from 2026-09-24.

## Global Constraints

- Do not add a second durable-task store or fabricate branch/source data.
- Keep the existing session panel dimensions and current cancel/resume endpoints.
- The conversation stream must not render a duplicate durable-task card.
- The active task is pinned; historical tasks are ordered by `created_at`, then `id`.

---

### Task 1: Add deterministic task presentation ordering

**Files:**
- Modify: `frontend/src/features/sessions/sessionState.ts`
- Test: `frontend/src/features/sessions/sessionState.test.ts`

- [ ] Add a failing test for active-first and creation-order sorting.
- [ ] Run the focused test and confirm it fails because the helper is absent.
- [ ] Add the pure helper and minimal types.
- [ ] Run the focused test and confirm it passes.

### Task 2: Extract single-step durable task detail

**Files:**
- Modify: `frontend/src/features/sessions/components/DurableTaskCard.tsx`
- Test: `frontend/src/features/sessions/durableTaskPresentation.test.tsx`

- [ ] Add a failing component test for rendering only the active step in compact mode.
- [ ] Run the focused test and confirm the current full-list behavior fails the assertion.
- [ ] Add a compact rendering prop while preserving existing full-card behavior.
- [ ] Run the focused component test.

### Task 3: Build the branch/source and persistent-task popover

**Files:**
- Create: `frontend/src/features/sessions/components/PersistentTaskSource.tsx`
- Modify: `frontend/src/features/sessions/presentation.tsx`
- Modify: `frontend/src/styles/sessions.css`

- [ ] Render branch/diff summary, source rows, and task summary rows.
- [ ] Pin the first active task and sort history with the helper.
- [ ] Open the selected task's compact detail below the source rows.
- [ ] Reuse existing cancel/resume callbacks and expose source links only when data exists.

### Task 4: Move session task loading and remove the conversation duplicate

**Files:**
- Modify: `frontend/src/features/sessions/SessionsPage.tsx`
- Modify: `frontend/src/types.ts`

- [ ] Load `/tasks` alongside the existing active-task endpoint.
- [ ] Pass task history, active task, refresh, cancel, and resume callbacks to the child panel.
- [ ] Remove the inline `DurableTaskCard` from the message stream.
- [ ] Keep active-task refresh after cancel/resume.

### Task 5: Verification

**Files:**
- Modify: `frontend/src/features/sessions/presentation.test.tsx` only if existing fixtures need the new props.

- [ ] Run focused Vitest tests.
- [ ] Run `npm run build` and `npm run lint` in `frontend`.
- [ ] Review the diff for unrelated changes and report any unresolved source-data limitations.

### Task 6: Direct durable-task resume without a user prompt

**Files:**
- Modify: `backend/src/tasks/state.py`
- Modify: `backend/src/api/routes/sessions.py`
- Modify: `backend/src/api/runtime.py`
- Modify: `frontend/src/features/sessions/SessionsPage.tsx`
- Test: `backend/tests/test_durable_task_resume.py`
- Test: `frontend/src/features/sessions/sessionState.test.ts`

- [ ] Add a backend helper that binds a newly staged recovery run to an explicit task id without inspecting message text.
- [ ] Add `POST /api/sessions/{session_id}/tasks/{task_id}/resume` that rejects non-resumable tasks or an already active session, creates only a recovery `Run`, commits it, and launches the coordinator.
- [ ] Add a regression test proving the direct resume path binds `Run.task_id` and does not insert a user message.
- [ ] Replace the frontend resume callback with the explicit resume endpoint and refresh task/run state from its response.
- [ ] Run backend and frontend focused tests, build, and lint.
