# PGAgent context architecture

This document defines the context protocol derived from Learn Claude Code and
Claw Code. It deliberately replaces the previous epoch, checkpoint, task-anchor,
and dynamic-preamble design.

## Invariants

1. Provider-visible history is append-only between context-budget operations.
   Ordinary turns never edit, move, or deduplicate prior messages. The bounded
   tool-result pass described below may replace only old `tool` message content
   with an immutable artifact preview; full compaction may replace an old
   conversation prefix.
2. The system prefix and tool schemas have deterministic content and order.
   Prompt-cache keys identify only that stable namespace; conversation state is
   never hashed into the key.
3. The latest ordinary user message is the active request. No task anchor is
   inserted during an uncompacted conversation.
4. Tool output remains verbatim while aggregate provider-visible tool-result
   content is at most 300,000 characters. Before every model turn, once that
   aggregate exceeds 300,000 characters, raw results are sorted largest-first
   and persisted until the replacement aggregate is at most 150,000 characters.
   Each replacement contains an approximately 2,000-character preview plus an
   artifact reference, and the model can page through the full current-session
   payload with `read_artifact`.
5. Tool calls and all of their results form one atomic protocol group.
6. Full compaction is the only operation allowed to replace a conversation
   prefix. It saves the full transcript, creates one continuation message, and
   keeps a recent verbatim tail without splitting a tool group.
7. A continuation message is one fixed nine-section checkpoint. The exact
   active request and canonical `DurableTask`/`PlanStep` state are merged into
   those sections; they are not emitted as separate task-anchor or todo tags.
   Any later ordinary user message is the new active request.
8. Persistent memory uses a model-routed, progressively disclosed protocol. A
   lightweight index of every active consolidated memory is frozen into each new
   root or delegated Run as a late system message. The model decides whether to
   call `MemorySearch` and `MemoryRead`; no program-selected Top-K is injected
   into a new user message. Legacy message snapshots remain replayable and are
   never rewritten. Skills remain lazy-loaded through tools.
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
5. Frozen persistent-memory router instructions and `memory_summary.md` index.
6. Append-only conversation messages in sequence order.

The memory router is provider-visible system context but is excluded from the
stable cache namespace. It is placed after the truly stable system/tool prefix,
so a newly published index diverges as late as possible. Consolidation is
asynchronous and ordinary conversation turns do not perform dynamic Top-K
injection.

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

## Tool-result character budget

1. The pass runs before threshold or provider-overflow full compaction is
   considered for each model turn. Full nine-section compaction always keeps
   its existing token threshold, provider-overflow, and explicit-request
   triggers; exhaustion of the tool-result pass does not force it.
2. The pass sums only provider `role=tool` content. At or below 300,000
   characters it returns the existing message list unchanged and creates no
   artifact.
3. Above 300,000 characters, unexternalized tool results are ordered by content
   length descending, excluding the two most recent tool results. Each older
   result whose complete preview wrapper is genuinely shorter is
   content-addressed in the session artifact store and replaced without
   changing its role, call id, name, or protocol position. Processing stops as
   soon as total tool-result content is at most 150,000 characters.
4. Every selected result retains up to 2,000 preview characters plus its
   artifact id. PGAgent does not shorten that preview further. Results too small
   for the wrapper to reduce aggregate size remain verbatim and create no
   artifact.
5. Already externalized previews are not shortened or nested into another
   artifact. If all effective raw-result replacements are exhausted while the
   aggregate remains above 150,000 characters, the runtime passes that provider
   view through unchanged. The append-only `ChatMessage` transcript remains the
   durable raw source used to rebuild later provider views, and the independent
   90,000-token context threshold will trigger nine-section compaction when
   reached.
6. The two most recent tool results always remain verbatim so the next model
   turn retains its freshest observations. They still count toward both the
   300,000-character trigger and the 150,000-character target. If those
   protected results make the target unreachable, rule 5 passes the maximally
   externalized view to the normal context-threshold logic.

## Cache contract

The cache namespace changes only when stable system instructions, workspace
rules, permission rules, enabled tool schemas, or selected Skill catalog change.
Todo updates, memory index publications, user messages, tool results, artifact
previews, and compaction summaries do not change the namespace. They extend or
replace only the dynamic suffix.

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

1. SQLite remains canonical for conversation history, Stage-1 rollout outputs,
   consolidated memories, jobs, source lineage, citations and usage feedback.
   Markdown files are individually atomic, read-only human projections and never
   feed back into SQLite. Runtime routing reads one SQLite snapshot, so it does
   not depend on cross-file Markdown publication order.
2. A completed accepted root Run creates a deferred extraction job. Startup and
   the watchdog activate jobs after the session has been idle for 60 seconds;
   when several deferred snapshots exist for one session, only the newest is
   extracted and older snapshots are marked superseded. Extraction never blocks
   terminal reply delivery and delegated child Runs do not create jobs.
3. Phase 1 reads the accepted transcript through its frozen end sequence. It
   keeps real user requests/corrections, assistant actions, tool calls/results,
   verification and cwd; it excludes injected instructions, runtime metadata,
   transient state and secrets. The extraction model applies a minimum-signal
   gate and writes retrieval-oriented `MemoryRollout` records with summary,
   raw memories, task groups, keywords, outcome and evidence.
4. Phase 2 consumes unselected Stage-1 records plus the visible existing
   handbook. The consolidation model merges duplicates, preserves cwd/project
   boundaries, resolves conflicts from evidence, archives obsolete guidance and
   records supporting rollout IDs. Program code validates scope and references,
   commits operations transactionally, then republishes each Markdown view with
   an atomic file replacement.
5. `memory_summary.md` is a complete lightweight router for all active memories,
   not a recent-N mini handbook. `MEMORY.md` contains consolidated bodies,
   `raw_memories.md` includes Stage-1 evidence, and `rollout_summaries/` contains
   detailed source recaps.
6. New root and delegated Runs build their scope-visible router directly from
   SQLite and freeze that slice as late system context. The global Markdown index
   remains a complete human-readable projection; it is not injected verbatim,
   which prevents another workspace or session from leaking into the Run. The
   model decides whether memory is relevant, chooses discriminative search terms,
   calls `MemorySearch`, and uses `MemoryRead` only for selected full entries or
   supporting rollout summaries. Search remains scope-bounded and deterministic;
   semantic routing belongs to the model.
7. If an accepted root answer actually used memory, it appends one hidden JSON
   citation block. Runtime removes it before verification/streaming, validates
   referenced memory and rollout IDs, stores exactly-once `MemoryCitation` rows,
   and updates `usage_count`/`last_usage_at`. Child citations receive no usage
   credit; the accepted parent must cite what affected its answer.
8. Phase-2 updates create new active versions and mark replaced records
   `superseded`; archival and forgetting are status transitions rather than
   physical deletion. Usage is one consolidation signal, not a sufficient rule
   for deleting new or rarely applicable knowledge.
9. `memory_settings.enabled` is the global persistent switch. When it is off,
   new Runs receive neither the memory router nor Memory tools, completed root
   Runs do not enqueue extraction, and deferred/pending jobs remain paused until
   it is enabled again. `sessions.use_memories` controls only whether that chat
   can read existing memory; it does not prevent an accepted chat from becoming
   future memory. Both values and the resulting router are frozen in the Run's
   `runtime_binding`, and delegated children inherit them, so an approval resume
   or an in-flight child cannot silently change policy midway through a Run.
