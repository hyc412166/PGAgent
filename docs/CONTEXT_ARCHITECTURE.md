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
   sees an immutable preview plus an artifact reference from the beginning.
5. Tool calls and all of their results form one atomic protocol group.
6. Full compaction is the only operation allowed to replace seen history. It
   saves the full transcript, creates one continuation message, and keeps a
   recent verbatim tail without splitting a tool group.
7. A continuation message contains the exact active request and exact TodoWrite
   snapshot at compaction time. Its prose summary is reference data, not a new
   instruction. Any later ordinary user message supersedes the request recorded
   by an older continuation.
8. Memories and skills are lazy-loaded through tools. Mutable memory content is
   not inserted ahead of conversation history.
9. Provider overflow performs one full compaction and one retry. It never falls
   back to arbitrary message trimming.

## Provider order

Without compaction:

1. Stable system rules.
2. Stable workspace rules.
3. Stable permission rules.
4. Stable agent instructions and Skill catalog.
5. Append-only conversation messages in sequence order.

After compaction:

1. The same stable prefix.
2. One user-role continuation message containing the exact active request,
   exact todo snapshot, summary, and transcript artifact reference.
3. Recent original messages, retained verbatim in atomic protocol groups.
4. Messages appended after compaction.

## Cache contract

The cache namespace changes only when stable system instructions, workspace
rules, permission rules, enabled tool schemas, or selected Skill catalog change.
Todo updates, memories, user messages, tool results, and compaction summaries do
not change the namespace. They extend or replace only the dynamic suffix.

