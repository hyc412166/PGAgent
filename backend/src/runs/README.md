# Durable run architecture

Run modules are separated from the model/tool loop:

- `lifecycle.py`: scheduling, stop ordering, durable status transitions,
  background wakeups and parent/child coordination.
- `runtime_factory.py`: builds stores, `ToolRegistry` and `AgentRuntime` from a
  resolved run context.
- `runtime_preparer.py`: attaches dynamic tool sources such as MCP after the
  base runtime exists.
- `continuation.py`: serializes/restores `RunOutcome` and captures runtime
  continuation state.
- `configuration.py`: resolves durable tool and model configuration.
- `delegation.py`: child-run delegation and recovery.
- `stream.py`: low-latency in-process stream delivery.

`RunCoordinator` owns a run, not a tool call. Tool lookup, authorization and
execution belong to `src/tools`; model iteration belongs to `src/agent`.
