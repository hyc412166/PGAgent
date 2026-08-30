# Coding workflow layer

`coding/` adds repository-change behavior to the shared `AgentRuntime`; it does not create a second runtime hierarchy.

- `profile.py` selects the direct coding/review tool surface and supplies workflow instructions.
- `patch.py` parses strict workspace-scoped multi-file text patches, stages replacements beside their targets and rolls back earlier files if a later commit fails.
- `validation.py` runs bounded checks through the existing command primitive and emits structured evidence.
- `state.py` is a post-invocation hook that records changed paths and validation outcomes in the resumable runtime binding; a later change marks older validation evidence stale.

Tool registration, authorization, sandboxing and deferred exposure remain owned by `tools/`. Prompt assembly remains owned by `agent/`. This keeps coding behavior composable with the normal run, approval and recovery lifecycle.
