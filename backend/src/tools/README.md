# Tool runtime architecture

The tool subsystem is split by lifecycle responsibility:

- `name.py`: canonical `ToolName` and namespace identity.
- `runtime.py`: one tool's schema, origin, presentation, execution metadata and executor.
- `registry.py`: deterministic runtime registration and lookup; legacy sync APIs remain compatibility facades.
- `plan.py`: the model-visible tool plan captured for one sampling request.
- `router.py`: provider wire-name parsing and creation of `ToolInvocation`.
- `invocation.py`: invocation values, approval grants and dispatch outcomes.
- `validation.py`: checks that must happen before an approval can be requested.
- `authorization.py`: the mandatory trusted permission hook.
- `hooks.py`: extension contracts for prepare, post and lifecycle hooks.
- `pipeline.py`: ordered hook, authorization and execution lifecycle.
- `scheduler.py`: parallel/ordered batch decisions based on runtime metadata.

Repository engineering primitives live in the sibling `coding/` domain. The tool
registry adapts `apply_patch`, `validate`, `review_finding`, and `debug_evidence`
into this lifecycle, while evidence recorders enter through normal post hooks. Long-running command
input is owned by `tasks/background.py` and exposed here as `write_stdin`.

Built-in, MCP and future dynamic tools must enter the runtime through
`ToolRegistry.register_runtime()` or a source adapter that calls it. Agent code
must dispatch through `ToolRouter`; it must not call a concrete executor or
select an approval path directly.

`ToolPresentation` uses composable properties instead of expanding an exposure
enum. `advertise_by_default`, `discoverable` and `model_callable` separately
describe the model surface.

For engineering-profile Agents, each workflow's core stays directly visible and
low-frequency built-ins use deferred presentation. `ToolSearch` with
`select:<tool-name>` activates them without changing the run's executable
capability boundary; activation state is restored from `runtime_binding`.

Authorization is a system hook. External hooks may reject or request stricter
approval, but they are not an authority that can bypass `AuthorizationHook`.
