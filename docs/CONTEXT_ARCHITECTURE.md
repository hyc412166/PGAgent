# PGAgent context architecture

This document defines the context protocol derived from Learn Claude Code and
Claw Code. It deliberately replaces the previous epoch, checkpoint, task-anchor,
and dynamic-preamble design.

## Invariants

1. Provider-visible history is append-only between full compactions. A message
   that the model has seen is never edited, shortened, moved, or deduplicated.
2. The system prefix and tool schemas have deterministic content and order.
   Prompt-cache keys identify only that stable namespace; conversation state is
   never hashed into the key.
3. The latest ordinary user message is the active request. No task anchor is
   inserted during an uncompacted conversation.
4. Large tool output is persisted before its first provider exposure. The model
   sees an immutable preview plus an artifact reference from the beginning and
   can page through the full current-session payload with `read_artifact`.
5. Tool calls and all of their results form one atomic protocol group.
6. Full compaction is the only operation allowed to replace seen history. It
   saves the full transcript, creates one continuation message, and keeps a
   recent verbatim tail without splitting a tool group.
7. A continuation message is one fixed nine-section checkpoint. The exact
   active request and canonical `DurableTask`/`PlanStep` state are merged into
   those sections; they are not emitted as separate task-anchor or todo tags.
   Any later ordinary user message is the new active request.
8. Persistent memories live in SQLite and are recalled only for a new user or
   delegated-task message. The selected records are rendered as background data
   inside that message and frozen in its provider payload before first model
   exposure. Later memory edits never rewrite an already-seen turn. Skills remain
   lazy-loaded through tools.
9. Provider overflow performs one full compaction and one retry. It never falls
   back to arbitrary message trimming.
10. A persistent teammate is a SQLite identity, not a long-lived provider
    session. Each assignment creates a fresh child Run and freezes that
    teammate's role prompt, unread mailbox, recent accepted assignment results,
    memory snapshot, tools, Skills, model and workspace/worktree into the new
    provider payload. Later messages are delivered only to a later assignment.

## Provider order

Without compaction:

1. Stable system rules.
2. Stable workspace rules. This message contains, in order, the global personal
   instructions selected from `data/AGENTS.override.md` or `data/AGENTS.md`, the
   selected workspace-root `AGENTS.override.md` or `AGENTS.md`, and the selected
   workspace boundary.
3. Stable permission rules.
4. Stable agent instructions and Skill catalog.
5. Append-only conversation messages in sequence order.

When relevant persistent memory exists, it is part of the current user message
at step 5, immediately before the original request. It never becomes a system
message and never changes the stable prefix.

After compaction:

1. The same stable prefix.
2. One user-role continuation message containing the nine-section summary and
   full-transcript artifact reference.
3. Recent original messages, retained verbatim in atomic protocol groups.
4. Messages appended after compaction.

The compaction model request itself is assembled as follows:

1. The exact original stable system prefix, without an added compactor identity
   or controller system message.
2. Only the old atomic conversation prefix selected for replacement, preserving
   its original user/assistant/tool roles and order.
3. One final user message instructing the model not to alter the system prompt,
   not to execute the conversation task, and to return exactly these sections:
   `Primary Request and Intent`, `User Corrections and Constraints`, `Completed
   Work`, `Current Work`, `Pending Tasks`, `Files and Code Sections`, `Technical
   Decisions and Problem Solving`, `Errors and Fixes`, and `Optional Next Step`.

The recent retained tail is never sent to the compaction model. The final user
instruction also supplies authoritative current task metadata. PGAgent validates
the exact nine-section shape, rejects extra Markdown headings or wrapper
delimiters, and mechanically rebuilds the current task-bearing sections from
the authoritative goal/status and full completed, in-progress, and
pending/recovery step records. With no current durable plan, sections 4 and 5
explicitly say that no inherited board is active. Therefore a fluent but stale
model summary cannot override the SQLite task checkpoint.

The configured compaction threshold is passed through `RuntimeConfig` and
clamped to the available input budget. Production compaction retains a suffix
of complete message groups within the configured 8,000-token budget. The newest
group is always retained even when that one group exceeds the budget; an
assistant tool-call batch is never split from any of its tool results.

## Artifact retrieval

1. Each runtime binds one `FilesystemArtifactStore` rooted at
   `data/artifacts/<session-id>/`; the context budgeter and `read_artifact` use
   the same store instance.
2. `read_artifact` accepts only an artifact id plus character `offset` and
   `limit`. It never exposes or accepts a server filesystem path.
3. A requested page is limited to 24,000 characters. PGAgent may return fewer
   characters when JSON escaping would otherwise push the serialized tool
   result to the 30,000-character externalization threshold. Response metadata
   reports the actual `next_offset`, `total_chars`, and `eof` for deterministic
   pagination.
4. An id absent from the current session store returns `artifact_not_found`.
   Workspace file tools remain unable to access PGAgent's internal data tree.
5. `read_artifact` is read-only and may execute in parallel with other
   read-only tools. Adding its schema causes the expected one-time stable tool
   cache namespace change.

## Cache contract

The cache namespace changes only when stable system instructions, workspace
rules, permission rules, enabled tool schemas, or selected Skill catalog change.
Todo updates, memories, user messages, tool results, and compaction summaries do
not change the namespace. They extend or replace only the dynamic suffix.

The compaction request uses the same stable cache namespace and original system
prefix. Its old conversation diverges only after that stable prefix; replacing
the main conversation with a summary necessarily starts a new dynamic suffix
but does not invalidate the stable system/tool namespace.

## Durable task-state alignment

1. `DurableTask` and ordered `PlanStep` rows in SQLite are the recovery source
   of truth. They retain the goal, task status, stable external step IDs,
   dependencies, executor assignment, completed/remaining work, next action,
   evidence and errors.
2. A successful `TodoWrite` updates the runtime board and synchronously projects
   it into those durable rows. Completed steps cannot be reopened by a later
   stale write.
3. During compaction, the runtime opens a fresh database transaction and reads
   the latest durable checkpoint. It does not rely on the `todo_state` frozen at
   Run creation. If that read fails, the compaction is rejected and the original
   messages remain in place rather than creating a degraded checkpoint.
4. Approval resume keeps the same Run and frozen runtime configuration, but a
   root Run with a durable task reloads its todo board from SQLite. The frozen
   todo snapshot remains only as compatibility data for legacy runs without a
   durable task.
5. A new ordinary user turn starts with an empty task board. It never inherits
   the previous Run's runtime snapshot. An explicit continuation turn binds to
   the latest resumable durable task and receives the backend-generated recovery
   packet.
6. At a terminal/interrupted boundary, Run status is projected back to the
   durable task. Interrupted active steps become `needs_recovery`; the next
   recovery Run must inspect real evidence before completing or retrying them.

Adding or removing a memory tool schema is a normal capability change and causes
one namespace change. Creating, updating, recalling, or consolidating memory
records does not.

## Personal and project instructions

1. The Personalization settings page reads and writes `data/AGENTS.md`. A
   non-empty `data/AGENTS.override.md` takes precedence, matching Codex's
   override behavior; the UI reports when that override is active.
2. PGAgent treats the selected workspace as both the project root and runtime
   working directory. It selects one non-empty instruction file there, checking
   `AGENTS.override.md` before `AGENTS.md`. It intentionally does not read parent
   directories outside the selected workspace boundary.
3. A newly created Run renders the global source before the project source and
   freezes both the rendered text and source paths in `Run.runtime_binding`.
   Approval resume and retry reuse that snapshot instead of rereading disk.
4. Editing an instruction file affects the next new Run, not an already running
   Run. Because the rendered chain is part of the stable workspace-rules
   message, a real instruction change naturally starts a new prompt-cache
   namespace; no separate conversation-state hash is added.
5. Project files are limited to 32 KiB during discovery. The settings API limits
   personal instructions to 32,768 characters and writes the file atomically.
   Reads and writes of the personalization API are loopback-only. Resolved
   instruction files must remain inside their global-data or selected-workspace
   boundary, so a symlink cannot import instructions from outside that boundary.

## Persistent-memory lifecycle

1. SQLite `memories` is the canonical store. Legacy `.memory/*.md` files are
   imported idempotently at startup and are not dual-written afterwards.
   After committed memory changes, PGAgent regenerates read-only views below
   `data/memories/`: `memory_summary.md`, `MEMORY.md`, `raw_memories.md`, and
   per-source-session files. These files never feed back into SQLite and are
   not used for replay, context assembly, or prompt-cache keys.
2. Recall searches only active global, current-workspace, and current-session
   records. Deterministic lexical relevance selects at most five bodies within
   a bounded character budget.
3. The accepted public user text remains unchanged. The exact provider rendering
   and selected record metadata are stored in `ChatMessage.provider_payload`.
4. Approval resume, transcript replay, and compaction consume that stored
   rendering. They never re-run recall for an old message.
5. After a deterministic accepted terminal response, a durable `memory_jobs`
   row performs auxiliary extraction. Its usage is stored on the job rather than
   in the conversation `UsageRecord`, so it cannot distort the main cache rate.
6. Updates create a new active row and mark the previous same-name record
   `superseded`; automatic consolidation does not physically delete history.
