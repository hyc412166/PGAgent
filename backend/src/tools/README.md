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

Built-in, MCP and future dynamic tools must enter the runtime through
`ToolRegistry.register_runtime()` or a source adapter that calls it. Agent code
must dispatch through `ToolRouter`; it must not call a concrete executor or
select an approval path directly.

`ToolPresentation` uses composable properties instead of expanding an exposure
enum. `advertise_by_default`, `discoverable` and `model_callable` separately
describe the model surface.

Authorization is a system hook. External hooks may reject or request stricter
approval, but they are not an authority that can bypass `AuthorizationHook`.
