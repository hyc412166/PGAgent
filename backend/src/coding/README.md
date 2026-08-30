# Engineering workflow layer

`coding/` adds Coding, Review, and Debug behavior to the shared `AgentRuntime`; it does not create a parallel runtime hierarchy.

- `profiles/` owns the small workflow policies. `coding`, `review`, and `debug` each define their direct tool surface and task-specific instructions; `resolver.py` handles explicit selection plus the legacy `auto` fallback.
- `evidence/` owns provider-callable `review_finding` / `debug_evidence` records and their resumable run-scoped ledger.
- `patch.py` parses strict workspace-scoped multi-file text patches, stages replacements beside their targets and rolls back earlier files if a later commit fails.
- `validation.py` runs bounded checks through the existing command primitive and emits structured evidence.
- `state.py` records changed paths and validation outcomes through a normal post-invocation hook; a later change marks older validation evidence stale.

`Agent.workflow_profile_id` is frozen into `runtime_binding`, and child delegation freezes the child's own profile. Tool registration, authorization, sandboxing and deferred exposure remain owned by `tools/`; prompt assembly remains owned by `agent/`. This keeps all three workflows composable with the normal run, approval and recovery lifecycle.

The Review profile hides configured mutating and orchestration tools from the model surface, including ToolSearch activation. This is a workflow capability ceiling: review evidence and targeted validation remain available, while repository changes require switching the Agent to Coding or Debug.
