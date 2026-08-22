"""LangGraph outer state machine for one PGAgent run."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, StateGraph


class RunGraphState(TypedDict, total=False):
    status: str
    mode: str
    messages: list[dict[str, Any]]
    events: list[dict[str, Any]]
    output: str | None
    error: str | None
    stop_reason: str | None
    pending_approval: dict[str, Any] | None
    current_made_progress: bool
    seen_observations: list[str]
    usage: dict[str, Any]
    context: dict[str, Any]
    compaction_state: dict[str, Any]
    context_artifact_refs: list[dict[str, Any]]
    transcript_delta: list[dict[str, Any]]
    verification_trace: list[dict[str, Any]]
    compaction_count: int
    context_overflow_retries: int
    completion_verification_attempts: int
    acceptance_report: dict[str, Any]
    prompt_cache_key: str


Node = Callable[[RunGraphState], RunGraphState | Awaitable[RunGraphState]]


def build_outer_graph(
    *,
    prepare_context: Node,
    act: Node,
    observe: Node,
    checkpointer: Any | None = None,
) -> Any:
    """Build the fixed safety lifecycle around a dynamic model/tool loop.

    Model behavior lives in ``act``. The graph only owns lifecycle transitions,
    which makes approval/checkpoint behavior inspectable and deterministic.
    """

    graph = StateGraph(RunGraphState)
    graph.add_node("preparing_context", prepare_context)
    graph.add_node("acting", act)
    graph.add_node("observing", observe)
    graph.add_node("awaiting_approval", lambda state: state)
    graph.set_entry_point("preparing_context")

    graph.add_conditional_edges(
        "preparing_context",
        lambda state: "acting" if state.get("status") == "acting" else "end",
        {"acting": "acting", "end": END},
    )
    graph.add_conditional_edges(
        "acting",
        lambda state: (
            "observing"
            if state.get("status") == "observing"
            else "awaiting_approval"
            if state.get("status") == "awaiting_approval"
            else "end"
        ),
        {"observing": "observing", "awaiting_approval": "awaiting_approval", "end": END},
    )
    graph.add_edge("awaiting_approval", END)
    graph.add_conditional_edges(
        "observing",
        lambda state: "acting" if state.get("status") == "acting" else "end",
        {"acting": "acting", "end": END},
    )
    return graph.compile(checkpointer=checkpointer)
