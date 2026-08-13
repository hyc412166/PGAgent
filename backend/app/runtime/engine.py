"""Claude-Code-style tool loop wrapped in PGAgent's LangGraph lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from .context import ContextBundle, ContextManager, message_tokens
from .acceptance import CompletionDecision
from .context_service import (
    ArtifactStore,
    ContextAssembler,
    ContextSnapshot,
    InMemoryArtifactStore,
    PromptLayout,
    SemanticCompactor,
    context_end_sequence,
)
from .errors import APIErrorKind, call_with_retry
from .graph import RunGraphState, build_outer_graph
from .guards import GuardDecision, LoopGuard
from ..tools import ToolRegistry
from ..tools.builtins import MAX_PARALLEL_DELEGATED_TASKS, normalize_delegate_requests
from ..tools.types import ToolResult


logger = logging.getLogger(__name__)


USAGE_COUNTER_KEYS = (
    "request_count",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "total_tokens",
)


_SECRET_ARGUMENT_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "token",
    "secret",
    "password",
    "cookie",
    "content",
    "old_string",
    "new_string",
)


def safe_tool_argument_summary(tool_name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build timeline-safe tool metadata without persisting private payloads.

    Tool arguments often contain an entire file body, command source code, or
    a URL query with a credential.  Timeline/SSE events intentionally receive
    only a short structural summary so the UI can show activity without
    turning the event log into a second secret store.
    """

    source = dict(arguments or {})
    summary: dict[str, Any] = {}
    for raw_key, value in source.items():
        key = str(raw_key)
        lowered = key.casefold()
        if any(marker in lowered for marker in _SECRET_ARGUMENT_MARKERS):
            summary[key] = "[redacted]"
            continue
        if key == "command":
            if isinstance(value, list) and value:
                summary[key] = {"executable": str(value[0])[:120], "argument_count": max(0, len(value) - 1)}
            elif isinstance(value, str):
                summary[key] = {"provided": True, "chars": len(value)}
            else:
                summary[key] = {"provided": bool(value)}
            continue
        if key == "url" and isinstance(value, str):
            try:
                parsed = urlsplit(value)
                summary[key] = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"[:300]
            except ValueError:
                summary[key] = {"provided": True, "chars": len(value)}
            continue
        if key == "todos" and isinstance(value, list):
            summary[key] = {"count": len(value)}
            continue
        if isinstance(value, str):
            # ``query`` and regular expressions can also contain private data;
            # activity needs the fact and size, not their literal text.
            summary[key] = {"chars": len(value)} if key in {"query", "pattern", "question", "task"} else value[:300]
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, (list, tuple, set)):
            summary[key] = {"count": len(value)}
        elif isinstance(value, Mapping):
            summary[key] = {"keys": sorted(str(item) for item in value)[:20]}
        else:
            summary[key] = {"type": type(value).__name__}
    # ``tool_name`` is emitted as a top-level event field by the caller; keep
    # this helper limited to the non-sensitive argument summary.
    return {"arguments": summary}


def safe_approval_request_summary(pending: Mapping[str, Any]) -> dict[str, Any]:
    """Redact an approval event while the durable Approval keeps its exact call."""

    tool_name = str(pending.get("tool_name") or "unknown")
    return {
        "id": str(pending.get("id") or ""),
        "tool_name": tool_name,
        **safe_tool_argument_summary(tool_name, pending.get("arguments") if isinstance(pending.get("arguments"), Mapping) else {}),
        "remaining_call_count": len(pending.get("remaining_calls") or []),
    }


def is_context_overflow_error(error: BaseException) -> bool:
    """Recognize provider context-window failures without retrying other 4xx errors."""

    if getattr(error, "context_overflow", False) is True:
        return True
    status = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    message = str(error).casefold()
    markers = (
        "context length",
        "context window",
        "maximum context",
        "max context",
        "too many tokens",
        "prompt is too long",
        "input is too long",
        "context_length_exceeded",
        "context_window_exceeded",
    )
    return bool(any(marker in message for marker in markers) and (status is None or 400 <= status < 500))


def empty_usage(
    *,
    model_connection_id: str | None = None,
    model_id: str = "",
    provider: str = "",
) -> dict[str, Any]:
    return {
        **{key: 0 for key in USAGE_COUNTER_KEYS},
        "cost_usd": 0.0,
        "model_connection_id": model_connection_id,
        "model_id": model_id,
        "provider": provider,
    }


def normalize_usage(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize provider or already-unified usage without trusting SDK types."""

    raw = dict(value or {})
    prompt_details = raw.get("prompt_tokens_details") or {}
    if not isinstance(prompt_details, Mapping):
        prompt_details = {}
    cache_creation = raw.get(
        "cache_creation_tokens",
        raw.get(
            "cache_creation_input_tokens",
            raw.get("prompt_cache_miss_tokens", raw.get("cache_miss_tokens", 0)),
        ),
    )
    cache_read = raw.get(
        "cache_read_tokens",
        raw.get(
            "cache_read_input_tokens",
            raw.get(
                "prompt_cache_hit_tokens",
                raw.get("cache_hit_tokens", prompt_details.get("cached_tokens", 0)),
            ),
        ),
    )

    def safe_int(candidate: Any) -> int:
        try:
            return max(0, int(candidate or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def safe_cost(candidate: Any) -> float:
        try:
            result = float(candidate or 0.0)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return result if math.isfinite(result) and result >= 0 else 0.0

    cache_creation_tokens = safe_int(cache_creation)
    cache_read_tokens = safe_int(cache_read)
    if "input_tokens" in raw:
        input_tokens = safe_int(raw.get("input_tokens"))
    else:
        # OpenAI-compatible prompt_tokens normally includes cached input. Keep
        # the unified categories disjoint while preserving raw total_tokens.
        input_tokens = max(
            0,
            safe_int(raw.get("prompt_tokens")) - cache_creation_tokens - cache_read_tokens,
        )
    output_tokens = safe_int(raw.get("output_tokens", raw.get("completion_tokens", 0)))
    total_tokens = safe_int(raw.get("total_tokens")) or (
        input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    )
    request_count = safe_int(raw.get("request_count"))
    if "request_count" not in raw and raw:
        request_count = 1
    return {
        "request_count": request_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "total_tokens": total_tokens,
        "cost_usd": safe_cost(raw.get("cost_usd", raw.get("cost", 0.0))),
        "model_connection_id": str(raw.get("model_connection_id") or "") or None,
        "model_id": str(raw.get("model_id") or raw.get("model") or ""),
        "provider": str(raw.get("provider") or ""),
    }


def merge_usage(current: Mapping[str, Any] | None, addition: Mapping[str, Any] | None) -> dict[str, Any]:
    aggregate = normalize_usage(current)
    increment = normalize_usage(addition)
    for key in USAGE_COUNTER_KEYS:
        aggregate[key] = int(aggregate[key]) + int(increment[key])
    aggregate["cost_usd"] = round(float(aggregate["cost_usd"]) + float(increment["cost_usd"]), 12)
    for key in ("model_connection_id", "model_id", "provider"):
        existing = aggregate[key]
        added = increment[key]
        if existing and added and existing != added:
            raise ValueError(f"运行期间模型身份发生变化：{key}")
        aggregate[key] = existing or added
    return aggregate


class SynchronousModelTimeout(TimeoutError):
    """A sync SDK timed out but its worker thread cannot be safely retried."""

    retryable = False


class RunTimeLimitExceeded(TimeoutError):
    """The cumulative active runtime crossed the configured safety fuse."""

    retryable = False


@dataclass(slots=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelTurn:
    content: str = ""
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, response: Any) -> "ModelTurn":
        if isinstance(response, cls):
            return response
        if not isinstance(response, Mapping):
            # LiteLLM/OpenAI objects expose model_dump in normal operation.
            if hasattr(response, "model_dump"):
                response = response.model_dump()
            else:
                raise TypeError("model_call 必须返回 ModelTurn、mapping 或支持 model_dump 的对象")

        raw: Mapping[str, Any] = response
        if raw.get("choices"):
            raw = raw["choices"][0].get("message", {})
        content = raw.get("content") or ""
        calls: list[ModelToolCall] = []
        for index, item in enumerate(raw.get("tool_calls") or []):
            function = item.get("function", item)
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            calls.append(
                ModelToolCall(
                    id=str(item.get("id") or f"call-{index + 1}"),
                    name=str(function.get("name", "")),
                    arguments=dict(arguments or {}),
                )
            )
        return cls(content=str(content), tool_calls=calls, usage=dict(response.get("usage") or {}))


@dataclass(slots=True)
class RuntimeConfig:
    max_steps: int | None = 0
    max_tool_calls: int | None = 0
    identical_call_limit: int = 3
    no_progress_limit: int = 4
    api_max_attempts: int = 3
    api_base_delay: float = 0.5
    model_timeout_seconds: float = 90.0
    max_run_seconds: float | None = 1_800.0
    observation_history_limit: int = 100_000
    event_sink_timeout_seconds: float = 5.0
    # LangGraph requires a recursion ceiling. Keep it outside any realistic
    # task size; application stopping is governed by the anti-loop signals.
    recursion_limit: int = 1_000_000
    # Context budgeting reserves room for the model response and provider
    # overhead.  Semantic compaction is bounded per run to prevent retry loops.
    context_output_reserve_tokens: int = 8_000
    context_safety_buffer_tokens: int = 2_000
    context_compaction_retain_tokens: int = 8_000
    max_compactions_per_run: int = 2
    max_completion_verification_attempts: int = 3


@dataclass(slots=True)
class RunOutcome:
    status: str
    output: str | None
    messages: list[dict[str, Any]]
    events: list[dict[str, Any]]
    steps: int
    tool_calls: int
    mode: str = "auto"
    stop_reason: str | None = None
    error: str | None = None
    pending_approval: dict[str, Any] | None = None
    guard_snapshot: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=empty_usage)
    active_elapsed_seconds: float = 0.0
    runtime_binding: dict[str, Any] = field(default_factory=dict)
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    # None means a legacy/manual outcome that did not provide an incremental
    # transcript; [] explicitly means there is nothing safe to persist.
    transcript_delta: list[dict[str, Any]] | None = None
    # Explicitly run-scoped protocol evidence. It is not inferred from session
    # history and therefore survives provider-side context compaction safely.
    verification_trace: list[dict[str, Any]] = field(default_factory=list)
    acceptance_report: dict[str, Any] = field(default_factory=dict)
    completion_verification_attempts: int = 0


ModelCall = Callable[..., Any | Awaitable[Any]]
EventSink = Callable[[dict[str, Any]], Any | Awaitable[Any]]
CompletionVerifier = Callable[[dict[str, Any]], CompletionDecision | Awaitable[CompletionDecision]]


class AgentRuntime:
    """Run one model-controlled loop with deterministic local safety rails."""

    def __init__(
        self,
        *,
        model_call: ModelCall,
        tool_registry: ToolRegistry,
        context_manager: ContextManager | None = None,
        event_sink: EventSink | None = None,
        stream_sink: EventSink | None = None,
        config: RuntimeConfig | None = None,
        checkpointer: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        context_assembler: ContextAssembler | None = None,
        semantic_compactor: SemanticCompactor | None = None,
        artifact_store: ArtifactStore | None = None,
        completion_verifier: CompletionVerifier | None = None,
    ) -> None:
        self.model_call = model_call
        try:
            model_signature = inspect.signature(model_call)
            accepts_var_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in model_signature.parameters.values()
            )
            self._model_accepts_delta = (
                "on_delta" in model_signature.parameters
                or accepts_var_kwargs
            )
            self._model_accepts_prompt_cache_key = (
                "prompt_cache_key" in model_signature.parameters or accepts_var_kwargs
            )
        except (TypeError, ValueError):
            self._model_accepts_delta = False
            self._model_accepts_prompt_cache_key = False
        self.tool_registry = tool_registry
        self.context_manager = context_manager or ContextManager()
        self.event_sink = event_sink
        self.stream_sink = stream_sink
        self.config = config or RuntimeConfig()
        self.checkpointer = checkpointer
        self.clock = clock
        self.completion_verifier = completion_verifier
        artifact_store = artifact_store or InMemoryArtifactStore()
        self.context_assembler = context_assembler or ContextAssembler(
            max_tokens=self.context_manager.max_tokens,
            output_reserve_tokens=self.config.context_output_reserve_tokens,
            safety_buffer_tokens=self.config.context_safety_buffer_tokens,
            artifact_store=artifact_store,
        )
        # By default semantic compaction uses the current conversation model.
        # It receives ``mode=compaction`` and an empty tool list, so no tool can
        # be executed during summarisation.
        self.semantic_compactor = semantic_compactor or SemanticCompactor(
            model_call=self.model_call,
            retain_tokens=self.config.context_compaction_retain_tokens,
            artifact_store=artifact_store,
        )

    async def _publish(self, state: RunGraphState, event_type: str, **payload: Any) -> list[dict[str, Any]]:
        event = {"type": event_type, **payload}
        events = [*state.get("events", []), event]
        if self.event_sink is not None:
            try:
                async def invoke_sink() -> None:
                    if inspect.iscoroutinefunction(self.event_sink):
                        result = self.event_sink(event)
                    else:
                        result = await asyncio.to_thread(self.event_sink, event)
                    if inspect.isawaitable(result):
                        await result

                await asyncio.wait_for(invoke_sink(), timeout=self.config.event_sink_timeout_seconds)
            except Exception as exc:
                # Persistence/telemetry must not strand an otherwise recoverable run
                # in "acting". Return a diagnostic in the outcome and log locally.
                logger.exception("PGAgent event sink failed for %s", event_type)
                events.append({
                    "type": "event_sink_failed",
                    "source_event": event_type,
                    "error": str(exc) or type(exc).__name__,
                    "error_kind": "timeout" if isinstance(exc, asyncio.TimeoutError) else "sink_error",
                })
        return events

    async def _publish_transient(self, event_type: str, **payload: Any) -> None:
        """Publish a UI-only event without adding it to the durable run log."""

        if self.stream_sink is None:
            return
        event = {"type": event_type, **payload}
        try:
            result = self.stream_sink(event)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=self.config.event_sink_timeout_seconds)
        except Exception:
            # Streaming telemetry is best-effort and must never fail a model
            # request or create a retry that duplicates visible output.
            logger.exception("PGAgent transient stream sink failed for %s", event_type)

    @staticmethod
    def _observation_fingerprint(tool_name: str, content: str) -> str:
        normalized = " ".join(content.split())
        return hashlib.sha256(f"{tool_name}\0{normalized}".encode("utf-8")).hexdigest()

    @staticmethod
    def _stop_state(state: RunGraphState, decision: GuardDecision) -> RunGraphState:
        return {
            **state,
            "status": "stopped",
            "stop_reason": decision.code,
            "error": decision.reason,
        }

    def _active_elapsed(self, prior_seconds: float, started_at: float) -> float:
        return max(0.0, prior_seconds) + max(0.0, self.clock() - started_at)

    def _run_time_decision(self, prior_seconds: float, started_at: float) -> GuardDecision:
        limit = self.config.max_run_seconds
        if limit and self._active_elapsed(prior_seconds, started_at) >= limit:
            return GuardDecision(
                True,
                "max_run_time",
                f"活动运行时间已达安全上限 {limit:g} 秒",
            )
        return GuardDecision.continue_()

    def _remaining_run_seconds(self, prior_seconds: float, started_at: float) -> float | None:
        limit = self.config.max_run_seconds
        if not limit:
            return None
        return max(0.0, limit - self._active_elapsed(prior_seconds, started_at))

    async def run(
        self,
        *,
        system_prompt: str,
        recent_messages: Sequence[Mapping[str, Any]],
        agent_instructions: str | None = None,
        workspace_rules: str | None = None,
        summary: str | None = None,
        memories: Sequence[str | Mapping[str, Any]] = (),
        tool_results: Sequence[Mapping[str, Any]] = (),
        mode: str = "auto",
        thread_id: str | None = None,
        prepared_messages: Sequence[Mapping[str, Any]] | None = None,
        prior_events: Sequence[Mapping[str, Any]] | None = None,
        guard_snapshot: Mapping[str, Any] | None = None,
        prior_usage: Mapping[str, Any] | None = None,
        prior_seen_observations: Sequence[str] = (),
        prior_active_elapsed_seconds: float = 0.0,
        context_snapshot: Mapping[str, Any] | None = None,
        permission_policy: str | None = None,
        task_state: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        context_sequence: int = 0,
        context_version: int = 0,
        prompt_cache_key_seed: str | None = None,
        artifact_refs: Sequence[Mapping[str, Any]] = (),
        task_anchor: str | None = None,
        transcript_delta: Sequence[Mapping[str, Any]] = (),
        prior_verification_trace: Sequence[Mapping[str, Any]] = (),
        prior_completion_verification_attempts: int = 0,
        prior_acceptance_report: Mapping[str, Any] | None = None,
    ) -> RunOutcome:
        """Execute until a final answer, approval pause, failure or safety stop."""

        # Old snapshots may still carry direct/plan. They resume under the new
        # auto policy rather than retaining a caller-selected planning mode.
        if mode in {"direct", "plan"}:
            mode = "auto"
        if mode != "auto":
            raise ValueError("mode 必须是 auto")
        if self.config.max_run_seconds is not None and self.config.max_run_seconds < 0:
            raise ValueError("max_run_seconds 必须大于等于 0")
        if self.config.observation_history_limit < 1:
            raise ValueError("observation_history_limit 必须大于 0")
        active_started_at = self.clock()
        active_elapsed_base = max(0.0, float(prior_active_elapsed_seconds or 0.0))
        observation_limit = self.config.observation_history_limit
        guard = LoopGuard(
            max_steps=self.config.max_steps,
            max_calls=self.config.max_tool_calls,
            identical_limit=self.config.identical_call_limit,
            no_progress_limit=self.config.no_progress_limit,
        )
        guard.restore(dict(guard_snapshot or {}))

        def render_instructions(context: Mapping[str, Any]) -> str:
            instructions = context.get("agent_instructions")
            auto_rule = "根据任务复杂度自行决定是否先在内部规则中规划；简单任务可直接执行。"
            rendered = f"{instructions}\n{auto_rule}" if instructions else auto_rule
            skill_catalog = self.tool_registry.skill_catalog_prompt
            return f"{rendered}\n{skill_catalog}" if skill_catalog else rendered

        def render_stable_prefix(context: Mapping[str, Any]) -> list[dict[str, Any]]:
            return ContextAssembler.stable_prefix(
                system_rules=context.get("system_prompt"),
                workspace_rules=context.get("workspace_rules"),
                permission_policy=context.get("permission_policy"),
                extra_messages=[{"role": "system", "content": render_instructions(context)}],
            )

        def messages_after_snapshot(
            dynamic: Sequence[Mapping[str, Any]],
            snapshot: ContextSnapshot | None,
        ) -> list[dict[str, Any]]:
            """Remove the retained checkpoint tail before appending new turns."""

            if snapshot is None or not snapshot.retained_messages:
                return [dict(item) for item in dynamic]
            retained = [dict(item) for item in snapshot.retained_messages]
            if len(dynamic) < len(retained):
                return [dict(item) for item in dynamic]
            for index, item in enumerate(retained):
                if json.dumps(dict(dynamic[index]), ensure_ascii=False, sort_keys=True, default=str) != json.dumps(item, ensure_ascii=False, sort_keys=True, default=str):
                    return [dict(entry) for entry in dynamic]
            return [dict(item) for item in dynamic[len(retained):]]

        async def prepare_node(state: RunGraphState) -> RunGraphState:
            if state.get("messages"):
                anchored_messages = self.context_manager.ensure_task_anchor(
                    state["messages"],
                    task=state.get("context", {}).get("task_anchor"),
                )
                events = await self._publish(
                    state,
                    "context_resumed",
                    estimated_tokens=None,
                    task_anchor_preserved=self.context_manager.has_task_anchor(anchored_messages),
                )
                return {
                    **state,
                    "status": "acting",
                    "messages": anchored_messages,
                    "events": events,
                }
            context = state["context"]
            instructions = context.get("agent_instructions")
            auto_rule = "根据任务复杂度自行决定是否先在内部规划；简单任务可直接执行。"
            instructions = f"{instructions}\n{auto_rule}" if instructions else auto_rule
            skill_catalog = self.tool_registry.skill_catalog_prompt
            if skill_catalog:
                instructions = f"{instructions}\n{skill_catalog}"
            # The legacy builder remains useful for tiny custom test budgets
            # and old snapshots. Production sessions use the new checkpoint
            # assembler so the stable rules stay byte-for-byte at the front.
            use_context_service = self.context_manager.max_tokens >= 4_096
            snapshot_payload = state.get("context_snapshot") or context.get("context_snapshot")
            snapshot: ContextSnapshot | None = None
            active_artifact_refs = [dict(item) for item in state.get("context_artifact_refs", [])]
            if use_context_service:
                stable_prefix = ContextAssembler.stable_prefix(
                    system_rules=context["system_prompt"],
                    workspace_rules=context.get("workspace_rules"),
                    permission_policy=context.get("permission_policy"),
                    extra_messages=[{"role": "system", "content": instructions}],
                )
                snapshot = (
                    ContextSnapshot.from_dict(snapshot_payload)
                    if isinstance(snapshot_payload, Mapping) and snapshot_payload.get("epoch_id")
                    else None
                )
                layout = self.context_assembler.assemble(
                    stable_prefix=stable_prefix,
                    snapshot=snapshot,
                    summary=context.get("summary"),
                    memories=context.get("memories", []),
                    task_anchor=context.get("task_anchor"),
                    task_state=context.get("task_state"),
                    recent_messages=[
                        *list(context.get("recent_messages", [])),
                        *list(context.get("tool_results", [])),
                    ],
                    artifact_refs=state.get("context_artifact_refs", []),
                    cache_key=context.get("prompt_cache_key_seed"),
                    max_tokens=self.context_assembler.input_budget,
                )
                bundle = ContextBundle(
                    messages=layout.messages,
                    estimated_tokens=layout.estimated_tokens,
                    omitted_messages=0,
                    truncated=layout.truncated,
                )
                active_artifact_refs = [ref.to_dict() for ref in layout.artifact_refs]
            else:
                bundle = self.context_manager.build(
                    system_prompt=context["system_prompt"],
                    agent_instructions=instructions,
                    workspace_rules=context.get("workspace_rules"),
                    summary=context.get("summary"),
                    memories=context.get("memories", []),
                    recent_messages=context.get("recent_messages", []),
                    tool_results=context.get("tool_results", []),
                    task_anchor=context.get("task_anchor"),
                )
            # Keep the provider-facing prompt cache prefix stable: all leading
            # system messages are fixed, while the conversation/checkpoint is
            # the dynamic suffix.  Small unit-test budgets intentionally stay
            # on the legacy assembler because no output reserve can fit.
            prompt_cache_key = str(context.get("prompt_cache_key") or "")
            tool_fingerprint = hashlib.sha256(
                json.dumps(
                    self.tool_registry.schemas,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
            stable_messages = stable_prefix if use_context_service else [
                item for item in bundle.messages if item.get("role") == "system"
            ]
            if not prompt_cache_key:
                cache_layout = self.context_assembler.assemble(
                    stable_prefix=stable_messages,
                    recent_messages=[item for item in bundle.messages if item.get("role") != "system"],
                    max_tokens=max(self.context_manager.max_tokens, 256),
                    micro_compact=False,
                )
                prompt_cache_key = f"{cache_layout.cache_key}:tools-{tool_fingerprint}"
            elif ":tools-" not in prompt_cache_key:
                prompt_cache_key = f"{prompt_cache_key}:tools-{tool_fingerprint}"
            compaction_count = int(state.get("compaction_count", 0) or 0)
            # Semantic compaction is performed before a provider turn, never
            # while a tool is executing.  The active conversation model is used
            # by SemanticCompactor by default; tools are disabled there.
            if (
                bundle.estimated_tokens >= self.semantic_compactor.retain_tokens
                and bundle.estimated_tokens >= self.context_assembler.compaction_threshold
                and compaction_count < self.config.max_compactions_per_run
                and len(bundle.messages) > 2
            ):
                compaction_started = await self._publish(
                    state,
                    "context_compaction_started",
                    reason="threshold",
                    phase="before_model",
                    before_tokens=bundle.estimated_tokens,
                    model="current_session_model",
                )
                try:
                    semantic_messages = [
                        item for item in bundle.messages if item.get("role") != "system"
                    ]
                    active_summary = (
                        snapshot.summary
                        if snapshot is not None
                        else context.get("summary")
                    )
                    active_task_state = (
                        snapshot.task_state
                        if snapshot is not None
                        else context.get("task_state")
                    )
                    result = await self.semantic_compactor.compact(
                        semantic_messages,
                        session_id=str(context.get("session_id") or ""),
                        sequence=context_end_sequence(
                            snapshot,
                            [
                                *list(context.get("recent_messages", [])),
                                *list(context.get("tool_results", [])),
                            ],
                        ),
                        existing_summary=active_summary if isinstance(active_summary, Mapping) else None,
                        task_state=active_task_state if isinstance(active_task_state, Mapping) else None,
                        reason="threshold",
                        artifact_refs=active_artifact_refs,
                        source_sequence=int(context.get("context_sequence") or 0),
                        version=int(context.get("context_version") or 0),
                    )
                    if result.epoch is not None and not result.ineffective:
                        snapshot_payload = result.epoch.to_dict()
                        snapshot = ContextSnapshot.from_epoch(result.epoch)
                        compacted_layout = self.context_assembler.assemble(
                            stable_prefix=stable_messages,
                            snapshot=snapshot,
                            memories=context.get("memories", []),
                            task_anchor=context.get("task_anchor"),
                            max_tokens=self.context_assembler.input_budget,
                            micro_compact=False,
                            cache_key=prompt_cache_key,
                        )
                        # Keep the concrete bundle type.  ``bundle`` is a
                        # ContextBundle in both the legacy and checkpoint
                        # paths; constructing its dynamic runtime type would
                        # fail exactly when a real semantic compaction fires.
                        bundle = ContextBundle(
                            messages=compacted_layout.messages,
                            estimated_tokens=compacted_layout.estimated_tokens,
                            omitted_messages=0,
                            truncated=compacted_layout.truncated,
                        )
                        compaction_count += 1
                        snapshot_payload = result.epoch.to_dict()
                        active_artifact_refs = [ref.to_dict() for ref in result.artifact_refs]
                    events = await self._publish(
                        {**state, "events": compaction_started},
                        "context_compaction_finished",
                        reason="threshold",
                        before_tokens=result.before_tokens,
                        after_tokens=result.after_tokens,
                        used_model=result.used_model,
                        fallback=result.fallback,
                        effective=not result.ineffective,
                        attempts=len(result.attempts),
                        context_snapshot=snapshot_payload,
                    )
                except Exception as exc:  # compaction must not destroy a run
                    events = await self._publish(
                        {**state, "events": compaction_started},
                        "context_compaction_failed",
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    state = {**state, "events": events}
                else:
                    state = {**state, "events": events}
            events = await self._publish(
                state,
                "context_prepared",
                estimated_tokens=bundle.estimated_tokens,
                omitted_messages=bundle.omitted_messages,
                task_anchor_preserved=self.context_manager.has_task_anchor(bundle.messages),
                prompt_cache_key=prompt_cache_key,
                context_epoch=(snapshot_payload or {}).get("epoch_id") if isinstance(snapshot_payload, Mapping) else None,
            )
            return {
                **state,
                "status": "acting",
                "messages": bundle.messages,
                "events": events,
                "context_snapshot": dict(snapshot_payload or {}),
                "context_artifact_refs": active_artifact_refs,
                "compaction_count": compaction_count,
                "prompt_cache_key": prompt_cache_key,
            }

        async def act_node(state: RunGraphState) -> RunGraphState:
            time_decision = self._run_time_decision(active_elapsed_base, active_started_at)
            if time_decision.stop:
                stopped = self._stop_state(state, time_decision)
                stopped["events"] = await self._publish(
                    stopped,
                    "run_stopped",
                    code=time_decision.code,
                    reason=time_decision.reason,
                )
                return stopped
            step_decision = guard.before_step()
            if step_decision.stop:
                stopped = self._stop_state(state, step_decision)
                stopped["events"] = await self._publish(stopped, "run_stopped", code=step_decision.code, reason=step_decision.reason)
                return stopped

            thought_started_at = self.clock()
            events = await self._publish(
                state,
                "model_step_started",
                step=guard.steps,
                elapsed_ms=round((thought_started_at - active_started_at) * 1000),
                monotonic_ms=round(thought_started_at * 1000),
            )
            state = {**state, "events": events}

            if self.context_manager.max_tokens >= 4_096:
                runtime_context = state.get("context") or {}
                stable = render_stable_prefix(runtime_context)
                raw_snapshot = state.get("context_snapshot") or runtime_context.get("context_snapshot") or {}
                active_snapshot = (
                    ContextSnapshot.from_dict(raw_snapshot)
                    if isinstance(raw_snapshot, Mapping) and raw_snapshot.get("epoch_id")
                    else None
                )
                dynamic_messages = [
                    dict(item) for item in state.get("messages", [])
                    if item.get("role") != "system"
                ]
                recent_messages = messages_after_snapshot(dynamic_messages, active_snapshot)
                layout = self.context_assembler.assemble(
                    stable_prefix=stable,
                    snapshot=active_snapshot,
                    summary=runtime_context.get("summary"),
                    memories=runtime_context.get("memories", []),
                    task_anchor=runtime_context.get("task_anchor"),
                    task_state=runtime_context.get("task_state"),
                    recent_messages=recent_messages,
                    artifact_refs=state.get("context_artifact_refs", []),
                    max_tokens=self.context_assembler.input_budget,
                    micro_compact=True,
                    cache_key=state.get("prompt_cache_key"),
                )
                state = {
                    **state,
                    "messages": layout.messages,
                    "context_artifact_refs": [ref.to_dict() for ref in layout.artifact_refs],
                }
                compaction_count = int(state.get("compaction_count", 0) or 0)
                if (
                    layout.estimated_tokens >= self.context_assembler.compaction_threshold
                    and compaction_count < self.config.max_compactions_per_run
                    and dynamic_messages
                ):
                    started = await self._publish(
                        state,
                        "context_compaction_started",
                        reason="threshold",
                        phase="before_model",
                        before_tokens=layout.estimated_tokens,
                        model="current_session_model",
                    )
                    try:
                        result = await self.semantic_compactor.compact(
                            dynamic_messages,
                            session_id=str(runtime_context.get("session_id") or ""),
                            sequence=context_end_sequence(active_snapshot, recent_messages),
                            existing_summary=(active_snapshot.summary if active_snapshot is not None else None),
                            task_state=(active_snapshot.task_state if active_snapshot is not None else runtime_context.get("task_state")),
                            reason="threshold",
                            artifact_refs=layout.artifact_refs,
                            source_sequence=int(runtime_context.get("context_sequence") or 0),
                            version=int(active_snapshot.version if active_snapshot is not None else runtime_context.get("context_version") or 0),
                        )
                        if result.epoch is not None and not result.ineffective:
                            promoted_snapshot = ContextSnapshot.from_epoch(result.epoch)
                            compacted_layout = self.context_assembler.assemble(
                                stable_prefix=stable,
                                snapshot=promoted_snapshot,
                                memories=runtime_context.get("memories", []),
                                task_anchor=runtime_context.get("task_anchor"),
                                recent_messages=[],
                                max_tokens=self.context_assembler.input_budget,
                                micro_compact=False,
                                cache_key=state.get("prompt_cache_key"),
                            )
                            state = {
                                **state,
                                "messages": compacted_layout.messages,
                                "context_snapshot": promoted_snapshot.to_dict(),
                                "context_artifact_refs": [ref.to_dict() for ref in result.artifact_refs],
                                "compaction_count": compaction_count + 1,
                            }
                        state["events"] = await self._publish(
                            {**state, "events": started},
                            "context_compaction_finished",
                            reason="threshold",
                            phase="before_model",
                            before_tokens=result.before_tokens,
                            after_tokens=result.after_tokens,
                            used_model=result.used_model,
                            fallback=result.fallback,
                            effective=not result.ineffective,
                            attempts=len(result.attempts),
                            context_snapshot=state.get("context_snapshot") or {},
                        )
                    except Exception as exc:  # compaction must not strand the turn
                        state["events"] = await self._publish(
                            {**state, "events": started},
                            "context_compaction_failed",
                            reason="threshold",
                            phase="before_model",
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
            else:
                trimmed_messages, omitted = self.context_manager.trim_runtime_messages(state.get("messages", []))
                if omitted:
                    state = {**state, "messages": trimmed_messages}
                    state["events"] = await self._publish(
                        state,
                        "context_compacted",
                        omitted_messages=omitted,
                        task_anchor_preserved=self.context_manager.has_task_anchor(trimmed_messages),
                    )

            async def retry_event(attempt: int, delay: float, kind: APIErrorKind, error: BaseException) -> None:
                state["events"] = await self._publish(
                    state,
                    "model_retry",
                    attempt=attempt,
                    delay_seconds=round(delay, 3),
                    error_kind=kind.value,
                    error_type=type(error).__name__,
                )

            async def recover_from_context_overflow(current_state: RunGraphState) -> RunGraphState | None:
                """Compact once after a provider overflow, then rebuild the same turn."""

                if self.context_manager.max_tokens < 4_096:
                    return None
                if int(current_state.get("context_overflow_retries", 0) or 0) >= 1:
                    return None
                context = current_state.get("context") or {}
                raw_snapshot = current_state.get("context_snapshot") or context.get("context_snapshot") or {}
                snapshot = (
                    ContextSnapshot.from_dict(raw_snapshot)
                    if isinstance(raw_snapshot, Mapping) and raw_snapshot.get("epoch_id")
                    else None
                )
                dynamic = [
                    item for item in current_state.get("messages", [])
                    if item.get("role") != "system"
                ]
                if not dynamic:
                    return None
                started = await self._publish(
                    current_state,
                    "context_compaction_started",
                    reason="provider_context_overflow",
                    phase="after_provider_overflow",
                    before_tokens=sum(message_tokens(item) for item in current_state.get("messages", [])),
                    model="current_session_model",
                )
                try:
                    stable = render_stable_prefix(context)
                    old_tokens = sum(message_tokens(item) for item in current_state.get("messages", []))
                    # The initial pre-turn semantic pass is the normal path.
                    # For an actual provider overflow, use a deterministic,
                    # single-shot emergency view: preserve stable rules and the
                    # newest complete dynamic item, never replaying a tool call.
                    # This path is deliberately model-free so a second model
                    # failure cannot create an overflow/compaction loop.
                    retained_dynamic = dynamic[-1:]
                    if retained_dynamic and retained_dynamic[0].get("role") == "tool":
                        for candidate in reversed(dynamic[:-1]):
                            if candidate.get("role") == "assistant" and candidate.get("tool_calls"):
                                retained_dynamic = [candidate, dynamic[-1]]
                                break
                    target_budget = max(256, min(self.context_assembler.input_budget, old_tokens // 2 or 256))
                    layout = self.context_assembler.assemble(
                        stable_prefix=stable,
                        snapshot=None,
                        summary=context.get("summary"),
                        memories=context.get("memories", []),
                        task_anchor=context.get("task_anchor"),
                        task_state=context.get("task_state"),
                        recent_messages=retained_dynamic,
                        artifact_refs=current_state.get("context_artifact_refs", []),
                        max_tokens=target_budget,
                        micro_compact=True,
                        cache_key=current_state.get("prompt_cache_key"),
                    )
                    if layout.estimated_tokens >= old_tokens:
                        # A provider may have a smaller hard limit than our
                        # estimate. Keep only the stable prefix plus a bounded
                        # final user message, fitting each message explicitly.
                        minimal: list[dict[str, Any]] = []
                        emergency_budget = max(64, old_tokens // 2)
                        remaining = emergency_budget
                        for item in [*stable[:1], *retained_dynamic[-1:]]:
                            fitted, _ = self.context_manager._fit_message(item, remaining)
                            if fitted is None:
                                continue
                            minimal.append(fitted)
                            remaining -= message_tokens(fitted)
                        if sum(message_tokens(item) for item in minimal) < old_tokens:
                            layout = PromptLayout(
                                stable_prefix=[item for item in minimal if item.get("role") == "system"],
                                dynamic_suffix=[item for item in minimal if item.get("role") != "system"],
                                cache_key=layout.cache_key,
                                cache_breakpoints=(sum(1 for item in minimal if item.get("role") == "system"),),
                                estimated_tokens=sum(message_tokens(item) for item in minimal),
                                truncated=True,
                            )
                    if layout.estimated_tokens >= old_tokens:
                        return None
                    finished = await self._publish(
                        {**current_state, "events": started},
                        "context_compaction_finished",
                        reason="provider_context_overflow",
                        before_tokens=sum(message_tokens(item) for item in current_state.get("messages", [])),
                        after_tokens=layout.estimated_tokens,
                        used_model=False,
                        fallback=True,
                        effective=True,
                        attempts=0,
                        context_snapshot=current_state.get("context_snapshot") or {},
                        fallback_error="provider_overflow_emergency_trim",
                    )
                    return {
                        **current_state,
                        "messages": layout.messages,
                        "context_snapshot": dict(current_state.get("context_snapshot") or {}),
                        "context_artifact_refs": [ref.to_dict() for ref in layout.artifact_refs],
                        "events": finished,
                        "compaction_count": int(current_state.get("compaction_count", 0) or 0) + 1,
                        "context_overflow_retries": 1,
                    }
                except Exception as exc:  # overflow recovery is best effort
                    failed = await self._publish(
                        {**current_state, "events": started},
                        "context_compaction_failed",
                        reason="provider_context_overflow",
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    return {**current_state, "events": failed}

            try:
                buffered_candidate_deltas: list[str] = []

                async def model_attempt() -> Any:
                    buffered_candidate_deltas.clear()

                    async def on_delta(delta: str) -> None:
                        if self.completion_verifier is not None:
                            buffered_candidate_deltas.append(delta)
                            return
                        await self._publish_transient(
                            "assistant_delta",
                            delta=delta,
                            step=guard.steps,
                        )

                    kwargs = {
                        "messages": state.get("messages", []),
                        "tools": self.tool_registry.schemas,
                        "mode": state.get("mode", "auto"),
                    }
                    if self._model_accepts_delta:
                        kwargs["on_delta"] = on_delta
                    if self._model_accepts_prompt_cache_key:
                        kwargs["prompt_cache_key"] = state.get("prompt_cache_key")
                    is_async_call = inspect.iscoroutinefunction(self.model_call)

                    async def invoke() -> Any:
                        if is_async_call:
                            result = self.model_call(**kwargs)
                        else:
                            # A synchronous SDK call must not block the event loop.
                            # Timed-out worker threads cannot be force-killed, but
                            # their eventual result is safely discarded.
                            result = await asyncio.to_thread(self.model_call, **kwargs)
                        if inspect.isawaitable(result):
                            return await result
                        return result

                    remaining_run_seconds = self._remaining_run_seconds(active_elapsed_base, active_started_at)
                    if remaining_run_seconds is not None and remaining_run_seconds <= 0:
                        raise RunTimeLimitExceeded("active runtime limit reached")
                    request_timeout = self.config.model_timeout_seconds
                    limited_by_run_fuse = False
                    if remaining_run_seconds is not None and remaining_run_seconds <= request_timeout:
                        request_timeout = remaining_run_seconds
                        limited_by_run_fuse = True
                    try:
                        return await asyncio.wait_for(invoke(), timeout=request_timeout)
                    except asyncio.TimeoutError as exc:
                        if (
                            limited_by_run_fuse
                            and self._run_time_decision(active_elapsed_base, active_started_at).stop
                        ):
                            raise RunTimeLimitExceeded("active runtime limit reached") from exc
                        if not is_async_call:
                            # Retrying would create concurrent orphan threads that may
                            # still bill or mutate external provider state.
                            raise SynchronousModelTimeout(
                                "同步模型调用超时；为避免并发遗留请求，本次不自动重试"
                            ) from exc
                        raise

                while True:
                    try:
                        response = await call_with_retry(
                            model_attempt,
                            max_attempts=self.config.api_max_attempts,
                            base_delay=self.config.api_base_delay,
                            on_retry=retry_event,
                        )
                        break
                    except Exception as exc:
                        if not is_context_overflow_error(exc):
                            raise
                        recovered = await recover_from_context_overflow(state)
                        if recovered is None:
                            raise
                        state = recovered
                turn = ModelTurn.from_response(response)
            except RunTimeLimitExceeded:
                decision = self._run_time_decision(active_elapsed_base, active_started_at)
                if not decision.stop:
                    decision = GuardDecision(
                        True,
                        "max_run_time",
                        f"活动运行时间已达安全上限 {self.config.max_run_seconds:g} 秒",
                    )
                stopped = self._stop_state(state, decision)
                stopped["events"] = await self._publish(
                    stopped,
                    "run_stopped",
                    code=decision.code,
                    reason=decision.reason,
                )
                return stopped
            except Exception as exc:
                failed = {**state, "status": "failed", "error": str(exc)}
                failed["events"] = await self._publish(
                    failed,
                    "model_failed",
                    error_type=type(exc).__name__,
                    elapsed_ms=round((self.clock() - active_started_at) * 1000),
                )
                return failed

            state = {**state, "usage": merge_usage(state.get("usage"), turn.usage)}
            time_decision = self._run_time_decision(active_elapsed_base, active_started_at)
            if time_decision.stop:
                stopped = self._stop_state(state, time_decision)
                stopped["events"] = await self._publish(
                    stopped,
                    "run_stopped",
                    code=time_decision.code,
                    reason=time_decision.reason,
                )
                return stopped
            assistant_message: dict[str, Any] = {"role": "assistant", "content": turn.content}
            if turn.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
                    }
                    for call in turn.tool_calls
                ]
            messages = [*state.get("messages", []), assistant_message]
            if not turn.tool_calls:
                if self.completion_verifier is not None:
                    attempt = int(state.get("completion_verification_attempts") or 0) + 1
                    state["events"] = await self._publish(
                        {**state, "messages": messages},
                        "completion_verification_started",
                        attempt=attempt,
                    )
                    try:
                        current_run_messages = [
                            *[dict(item) for item in state.get("verification_trace", [])],
                            dict(assistant_message),
                        ]
                        raw_decision = self.completion_verifier({
                            "output": turn.content,
                            "messages": current_run_messages,
                            "events": state.get("events", []),
                            "tool_calls": guard.calls,
                            "pending_approval": state.get("pending_approval"),
                            "attempt": attempt,
                        })
                        decision = await raw_decision if inspect.isawaitable(raw_decision) else raw_decision
                        if not isinstance(decision, CompletionDecision):
                            raise TypeError("completion_verifier must return CompletionDecision")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception("PGAgent completion verifier failed")
                        decision = CompletionDecision(
                            accepted=False,
                            reason=f"验收器执行失败：{type(exc).__name__}: {exc}",
                            report={"stage": "verifier", "error_type": type(exc).__name__},
                        )
                    state = {
                        **state,
                        "usage": merge_usage(state.get("usage"), decision.usage),
                        "completion_verification_attempts": attempt,
                        "acceptance_report": dict(decision.report),
                    }
                    event_type = (
                        "completion_verification_passed"
                        if decision.accepted
                        else "completion_verification_rejected"
                    )
                    state["events"] = await self._publish(
                        state,
                        event_type,
                        attempt=attempt,
                        accepted=decision.accepted,
                    )
                    if not decision.accepted:
                        limit = max(1, int(self.config.max_completion_verification_attempts or 1))
                        if attempt >= limit:
                            reason = f"候选结果连续 {attempt} 次未通过验收，详见验收报告"
                            stopped = {
                                **state,
                                "status": "stopped",
                                "messages": [
                                    item for item in state.get("messages", [])
                                    if not str(item.get("content") or "").startswith("[内部验收反馈")
                                ],
                                "stop_reason": "acceptance_failed",
                                "error": reason,
                                "output": None,
                            }
                            stopped["events"] = await self._publish(
                                stopped,
                                "run_stopped",
                                code="acceptance_failed",
                                reason=reason,
                                attempts=attempt,
                            )
                            return stopped
                        feedback = {
                            # Dynamic system messages are intentionally rebuilt
                            # from the stable prefix before every model call.
                            # Keep verifier feedback as an internal user-side
                            # observation so it survives that rebuild; it is not
                            # added to transcript_delta and never becomes a
                            # durable/user-authored chat row.
                            "role": "user",
                            "content": (
                                "[内部验收反馈，不是新的用户请求]\n"
                                "独立验收器拒绝了上一版候选答复。不要直接重复原答案；请根据以下反馈继续检查、"
                                "补充证据、修复问题后重新提交候选结果。\n"
                                f"验收反馈：{str(decision.reason or '未达到验收标准')[:8_000]}"
                            ),
                        }
                        return {
                            **state,
                            "status": "observing",
                            "messages": [*state.get("messages", []), feedback],
                            "current_made_progress": True,
                        }
                state = {
                    **state,
                    "transcript_delta": [*state.get("transcript_delta", []), dict(assistant_message)],
                }
                if self.completion_verifier is not None and turn.content:
                    # Candidate text was buffered while the provider streamed.
                    # Publish it only after both acceptance layers pass.
                    await self._publish_transient(
                        "assistant_delta",
                        delta=turn.content,
                        step=guard.steps,
                        accepted=True,
                    )
                clean_history = [
                    item for item in state.get("messages", [])
                    if not str(item.get("content") or "").startswith("[内部验收反馈")
                ]
                completed = {
                    **state,
                    "status": "completed",
                    "output": turn.content,
                    "messages": [*clean_history, assistant_message],
                }
                completed["events"] = await self._publish(
                    completed,
                    "run_completed",
                    usage=completed["usage"],
                    has_output=bool(turn.content),
                    output_chars=len(turn.content),
                    elapsed_ms=round((self.clock() - active_started_at) * 1000),
                    thought_duration_ms=round((self.clock() - thought_started_at) * 1000),
                )
                return completed

            state = {
                **state,
                "transcript_delta": [*state.get("transcript_delta", []), dict(assistant_message)],
                "verification_trace": [*state.get("verification_trace", []), dict(assistant_message)],
            }
            made_progress = False
            seen = list(state.get("seen_observations", []))
            seen_set = set(seen)
            parallel_results: list[ToolResult] | None = None
            parallel_tool_started: dict[str, float] = {}
            parallel_child_waits: list[ToolResult] = []
            turn_delegate_count = 0
            for call in turn.tool_calls:
                if call.name != "task":
                    continue
                requests, _, _ = normalize_delegate_requests(
                    call.arguments.get("task"),
                    call.arguments.get("agent_id"),
                    call.arguments.get("tasks"),
                )
                turn_delegate_count += len(requests)
            turn_delegate_limit_exceeded = turn_delegate_count > MAX_PARALLEL_DELEGATED_TASKS
            # A provider may return several independent task calls in one
            # turn.  Dispatch those calls together; ordinary tools remain in
            # their existing ordered path because they may depend on one
            # another's workspace changes.
            parallel_task_turn = (
                self.tool_registry.permission_mode != "ask"
                and len(turn.tool_calls) > 1
                and all(
                call.name == "task" for call in turn.tool_calls
                )
            )
            if parallel_task_turn:
                for call in turn.tool_calls:
                    time_decision = self._run_time_decision(active_elapsed_base, active_started_at)
                    if time_decision.stop:
                        stopped = self._stop_state({**state, "messages": messages}, time_decision)
                        stopped["events"] = await self._publish(
                            stopped,
                            "run_stopped",
                            code=time_decision.code,
                            reason=time_decision.reason,
                        )
                        return stopped
                    call_decision = guard.before_tool_call(call.name, call.arguments)
                    if call_decision.stop:
                        stopped = self._stop_state({**state, "messages": messages}, call_decision)
                        stopped["events"] = await self._publish(
                            stopped,
                            "run_stopped",
                            code=call_decision.code,
                            reason=call_decision.reason,
                        )
                        return stopped
                    tool_started_at = self.clock()
                    parallel_tool_started[call.id] = tool_started_at
                    state["events"] = await self._publish(
                        {**state, "messages": messages},
                        "tool_started",
                        tool_name=call.name,
                        tool_call_id=call.id,
                        **safe_tool_argument_summary(call.name, call.arguments),
                        elapsed_ms=round((tool_started_at - active_started_at) * 1000),
                        thought_duration_ms=round((tool_started_at - thought_started_at) * 1000),
                    )

                if turn_delegate_limit_exceeded:
                    parallel_results = [
                        ToolResult(
                            "task",
                            False,
                            f"同一轮最多并行委派 {MAX_PARALLEL_DELEGATED_TASKS} 个子 Agent 任务",
                            error_code="delegate_parallel_limit",
                        )
                        for _call in turn.tool_calls
                    ]
                else:
                    raw_parallel_results = await asyncio.gather(
                        *(
                            self.tool_registry.execute_async(
                                call.name,
                                call.arguments,
                                approved=False,
                                call_id=call.id,
                            )
                            for call in turn.tool_calls
                        ),
                        return_exceptions=True,
                    )
                    parallel_results = [
                        result if isinstance(result, ToolResult) else ToolResult(
                        "task",
                        False,
                        f"工具执行失败: {type(result).__name__}",
                        error_code="tool_error",
                        )
                        for result in raw_parallel_results
                    ]
            for call_index, call in enumerate(turn.tool_calls):
                if parallel_results is None:
                    time_decision = self._run_time_decision(active_elapsed_base, active_started_at)
                    if time_decision.stop:
                        stopped = self._stop_state({**state, "messages": messages}, time_decision)
                        stopped["events"] = await self._publish(
                            stopped,
                            "run_stopped",
                            code=time_decision.code,
                            reason=time_decision.reason,
                        )
                        return stopped
                    call_decision = guard.before_tool_call(call.name, call.arguments)
                    if call_decision.stop:
                        stopped = self._stop_state({**state, "messages": messages}, call_decision)
                        stopped["events"] = await self._publish(stopped, "run_stopped", code=call_decision.code, reason=call_decision.reason)
                        return stopped

                    tool_started_at = self.clock()
                    state["events"] = await self._publish(
                        {**state, "messages": messages},
                        "tool_started",
                        tool_name=call.name,
                        tool_call_id=call.id,
                        **safe_tool_argument_summary(call.name, call.arguments),
                        elapsed_ms=round((tool_started_at - active_started_at) * 1000),
                        thought_duration_ms=round((tool_started_at - thought_started_at) * 1000),
                    )

                    # Approval is never inferred from a model-controlled call id. The
                    # only grant path is resume_after_approval's persisted exact call.
                    if call.name == "task" and turn_delegate_limit_exceeded:
                        result = ToolResult(
                            "task",
                            False,
                            f"同一轮最多并行委派 {MAX_PARALLEL_DELEGATED_TASKS} 个子 Agent 任务",
                            error_code="delegate_parallel_limit",
                        )
                    else:
                        result = await self.tool_registry.execute_async(
                            call.name,
                            call.arguments,
                            approved=False,
                            call_id=call.id,
                        )
                else:
                    result = parallel_results[call_index]
                    tool_started_at = parallel_tool_started.get(call.id, self.clock())
                if result.approval_required:
                    state["events"] = await self._publish(
                        {**state, "messages": messages},
                        "tool_finished",
                        tool_name=call.name,
                        tool_call_id=call.id,
                        ok=False,
                        changed=False,
                        error_code=result.error_code,
                        pending_approval=True,
                        duration_ms=round((self.clock() - tool_started_at) * 1000),
                        elapsed_ms=round((self.clock() - active_started_at) * 1000),
                    )
                    if result.approval_request is not None:
                        result.approval_request.id = call.id
                    pending = result.approval_request.to_dict() if result.approval_request else {
                        "id": call.id,
                        "tool_name": call.name,
                        "arguments": call.arguments,
                    }
                    pending["remaining_calls"] = [
                        {"id": remaining.id, "name": remaining.name, "arguments": remaining.arguments}
                        for remaining in turn.tool_calls[call_index + 1 :]
                    ]
                    pending["batch_made_progress"] = made_progress
                    pending["seen_observations"] = seen[-observation_limit:]
                    waiting = {
                        **state,
                        "status": "awaiting_approval",
                        "messages": messages,
                        "pending_approval": pending,
                    }
                    waiting["events"] = await self._publish(
                        waiting,
                        "approval_requested",
                        request=safe_approval_request_summary(pending),
                    )
                    time_decision = self._run_time_decision(active_elapsed_base, active_started_at)
                    if time_decision.stop:
                        stopped = self._stop_state(waiting, time_decision)
                        stopped["pending_approval"] = None
                        stopped["events"] = await self._publish(
                            stopped,
                            "run_stopped",
                            code=time_decision.code,
                            reason=time_decision.reason,
                        )
                        return stopped
                    return waiting

                result_dict = result.to_dict()
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps(result_dict, ensure_ascii=False),
                }
                messages.append(tool_message)
                state = {
                    **state,
                    "transcript_delta": [*state.get("transcript_delta", []), dict(tool_message)],
                    "verification_trace": [*state.get("verification_trace", []), dict(tool_message)],
                }
                fingerprint = self._observation_fingerprint(call.name, result.content)
                observation_is_new = fingerprint not in seen_set
                if observation_is_new:
                    seen.append(fingerprint)
                    seen_set.add(fingerprint)
                # Failed calls are not progress. Successful repeated observations are
                # also not progress unless the tool explicitly changed workspace state.
                made_progress = made_progress or result.changed or (result.ok and observation_is_new)
                state["events"] = await self._publish(
                    {**state, "messages": messages},
                    "tool_finished",
                    tool_name=call.name,
                    tool_call_id=call.id,
                    ok=result.ok,
                    changed=result.changed,
                    error_code=result.error_code,
                    duration_ms=round((self.clock() - tool_started_at) * 1000),
                    elapsed_ms=round((self.clock() - active_started_at) * 1000),
                )
                if result.metadata.get("delegated_child_awaiting_approval"):
                    if parallel_results is not None:
                        parallel_child_waits.append(result)
                        continue
                    child_run_id = str(result.metadata.get("child_run_id") or "")
                    task_id = str(result.metadata.get("task_id") or "")
                    reason = "A delegated child run is awaiting user approval."
                    stopped = {
                        **state,
                        "status": "stopped",
                        "messages": messages,
                        "stop_reason": "delegated_child_awaiting_approval",
                        "error": reason,
                    }
                    stopped["events"] = await self._publish(
                        stopped,
                        "run_stopped",
                        code="delegated_child_awaiting_approval",
                        reason=reason,
                        child_run_id=child_run_id,
                        task_id=task_id,
                        elapsed_ms=round((self.clock() - active_started_at) * 1000),
                    )
                    return stopped
                if result.metadata.get("needs_user_input"):
                    question = str(result.metadata.get("question") or result.content).strip()
                    asking = {**state, "messages": messages, "events": state.get("events", [])}
                    asking["events"] = await self._publish(
                        asking,
                        "user_question_requested",
                        tool_name=call.name,
                        tool_call_id=call.id,
                        question_chars=len(question),
                        requires_next_message=True,
                        elapsed_ms=round((self.clock() - active_started_at) * 1000),
                    )
                    stopped = {
                        **asking,
                        "status": "stopped",
                        "output": f"我需要先确认：{question}",
                        "stop_reason": "needs_user_input",
                    }
                    stopped["events"] = await self._publish(
                        stopped,
                        "run_stopped",
                        code="needs_user_input",
                        reason="Agent requires user input before completion",
                        elapsed_ms=round((self.clock() - active_started_at) * 1000),
                    )
                    return stopped
                time_decision = self._run_time_decision(active_elapsed_base, active_started_at)
                if time_decision.stop:
                    stopped = self._stop_state({**state, "messages": messages}, time_decision)
                    stopped["events"] = await self._publish(
                        stopped,
                        "run_stopped",
                        code=time_decision.code,
                        reason=time_decision.reason,
                    )
                    return stopped
            if parallel_child_waits:
                waiting_children = [
                    {
                        "child_run_id": str(result.metadata.get("child_run_id") or ""),
                        "task_id": str(result.metadata.get("task_id") or ""),
                    }
                    for result in parallel_child_waits
                ]
                reason = "One or more delegated child runs are awaiting user approval."
                stopped = {
                    **state,
                    "status": "stopped",
                    "messages": messages,
                    "stop_reason": "delegated_child_awaiting_approval",
                    "error": reason,
                }
                stopped["events"] = await self._publish(
                    stopped,
                    "run_stopped",
                    code="delegated_child_awaiting_approval",
                    reason=reason,
                    waiting_children=waiting_children,
                    elapsed_ms=round((self.clock() - active_started_at) * 1000),
                )
                return stopped
            return {
                **state,
                "status": "observing",
                "messages": messages,
                "events": state.get("events", []),
                "current_made_progress": made_progress,
                "seen_observations": seen[-observation_limit:],
            }

        async def observe_node(state: RunGraphState) -> RunGraphState:
            decision = guard.record_progress(bool(state.get("current_made_progress")))
            if decision.stop:
                stopped = self._stop_state(state, decision)
                stopped["events"] = await self._publish(stopped, "run_stopped", code=decision.code, reason=decision.reason)
                return stopped
            return {**state, "status": "acting", "current_made_progress": False}

        graph = build_outer_graph(
            prepare_context=prepare_node,
            act=act_node,
            observe=observe_node,
            checkpointer=self.checkpointer,
        )
        initial: RunGraphState = {
            "status": "received",
            "mode": mode,
            "messages": [dict(item) for item in (prepared_messages or [])],
            "events": [dict(item) for item in prior_events] if prior_events is not None else [{"type": "run_received", "mode": mode}],
            "seen_observations": [str(item) for item in prior_seen_observations][-observation_limit:],
            "usage": normalize_usage(prior_usage),
            "context": {
                "system_prompt": system_prompt,
                "agent_instructions": agent_instructions,
                "workspace_rules": workspace_rules,
                "summary": summary,
                "memories": list(memories),
                "recent_messages": [dict(item) for item in recent_messages],
                "tool_results": [dict(item) for item in tool_results],
                "task_anchor": task_anchor or self.context_manager.task_from_messages(recent_messages),
                "context_snapshot": dict(context_snapshot or {}),
                "permission_policy": permission_policy,
                "task_state": dict(task_state or {}),
                "session_id": session_id,
                "context_sequence": max(0, int(context_sequence or 0)),
                "context_version": max(0, int(context_version or 0)),
                "prompt_cache_key_seed": prompt_cache_key_seed,
            },
            "context_snapshot": dict(context_snapshot or {}),
            "context_artifact_refs": [dict(item) for item in artifact_refs],
            "transcript_delta": [dict(item) for item in transcript_delta],
            "verification_trace": [dict(item) for item in prior_verification_trace],
            "compaction_count": 0,
            "context_overflow_retries": 0,
            "completion_verification_attempts": max(0, int(prior_completion_verification_attempts or 0)),
            "acceptance_report": dict(prior_acceptance_report or {}),
        }
        graph_config: dict[str, Any] = {"recursion_limit": self.config.recursion_limit}
        if self.checkpointer is not None:
            graph_config["configurable"] = {"thread_id": thread_id or str(uuid4())}
        final: RunGraphState = await graph.ainvoke(initial, config=graph_config)
        return RunOutcome(
            status=final.get("status", "failed"),
            output=final.get("output"),
            messages=final.get("messages", []),
            events=final.get("events", []),
            steps=guard.steps,
            tool_calls=guard.calls,
            mode=mode,
            stop_reason=final.get("stop_reason"),
            error=final.get("error"),
            pending_approval=final.get("pending_approval"),
            guard_snapshot=guard.snapshot(),
            usage=normalize_usage(final.get("usage")),
            active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
            context_snapshot=dict(final.get("context_snapshot") or {}),
            artifact_refs=[dict(item) for item in final.get("context_artifact_refs", [])],
            transcript_delta=[dict(item) for item in final.get("transcript_delta", [])],
            verification_trace=[dict(item) for item in final.get("verification_trace", [])],
            acceptance_report=dict(final.get("acceptance_report") or {}),
            completion_verification_attempts=max(0, int(final.get("completion_verification_attempts") or 0)),
        )

    async def resume_after_approval(
        self,
        prior: RunOutcome,
        *,
        thread_id: str | None = None,
        runtime_context: Mapping[str, Any] | None = None,
    ) -> RunOutcome:
        """Execute the exact persisted pending call, then continue from its messages."""

        if prior.status != "awaiting_approval" or not prior.pending_approval:
            raise ValueError("只有 awaiting_approval 运行可以恢复")
        if self.config.max_run_seconds is not None and self.config.max_run_seconds < 0:
            raise ValueError("max_run_seconds 必须大于等于 0")
        if self.config.observation_history_limit < 1:
            raise ValueError("observation_history_limit 必须大于 0")
        active_started_at = self.clock()
        active_elapsed_base = max(0.0, float(prior.active_elapsed_seconds or 0.0))
        observation_limit = self.config.observation_history_limit
        pending = prior.pending_approval
        resume_transcript_delta: list[dict[str, Any]] = []
        resume_verification_trace = [dict(item) for item in prior.verification_trace]
        call_id = str(pending.get("id", ""))
        tool_name = str(pending.get("tool_name", ""))
        arguments = dict(pending.get("arguments") or {})
        if not call_id or not tool_name:
            raise ValueError("审批请求缺少 id 或 tool_name")

        def elapsed_ms() -> int:
            return round((active_elapsed_base + max(0.0, self.clock() - active_started_at)) * 1000)

        restored = LoopGuard(
            max_steps=self.config.max_steps,
            max_calls=self.config.max_tool_calls,
            identical_limit=self.config.identical_call_limit,
            no_progress_limit=self.config.no_progress_limit,
        )
        restored.restore(prior.guard_snapshot)

        async def timeout_outcome(
            current_events: list[dict[str, Any]],
            current_messages: list[dict[str, Any]],
        ) -> RunOutcome | None:
            decision = self._run_time_decision(active_elapsed_base, active_started_at)
            if not decision.stop:
                return None
            stopped_events = await self._publish(
                {"events": current_events},
                "run_stopped",
                code=decision.code,
                reason=decision.reason,
            )
            return RunOutcome(
                status="stopped",
                output=None,
                messages=current_messages,
                events=stopped_events,
                steps=restored.steps,
                tool_calls=restored.calls,
                mode=prior.mode,
                stop_reason=decision.code,
                error=decision.reason,
                guard_snapshot=restored.snapshot(),
                usage=normalize_usage(prior.usage),
                active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
                context_snapshot=dict(prior.context_snapshot),
                artifact_refs=[dict(item) for item in prior.artifact_refs],
                transcript_delta=[dict(item) for item in resume_transcript_delta],
                verification_trace=[dict(item) for item in resume_verification_trace],
                acceptance_report=dict(prior.acceptance_report),
                completion_verification_attempts=prior.completion_verification_attempts,
            )

        resume_state: RunGraphState = {"events": list(prior.events)}
        timed_out = await timeout_outcome(list(prior.events), list(prior.messages))
        if timed_out is not None:
            return timed_out
        events = await self._publish(
            resume_state,
            "approval_granted",
            request_id=call_id,
            tool_name=tool_name,
            elapsed_ms=elapsed_ms(),
        )
        resume_state["events"] = events
        timed_out = await timeout_outcome(events, list(prior.messages))
        if timed_out is not None:
            return timed_out
        tool_started_at = self.clock()
        events = await self._publish(
            {"events": events},
            "tool_started",
            tool_name=tool_name,
            tool_call_id=call_id,
            resumed_after_approval=True,
            **safe_tool_argument_summary(tool_name, arguments),
            elapsed_ms=elapsed_ms(),
        )
        result = await self.tool_registry.execute_async(
            tool_name,
            arguments,
            approved=True,
            call_id=call_id,
        )
        approved_tool_message = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": tool_name,
            "content": json.dumps(result.to_dict(), ensure_ascii=False),
        }
        messages = [*prior.messages, approved_tool_message]
        resume_transcript_delta.append(dict(approved_tool_message))
        resume_verification_trace.append(dict(approved_tool_message))
        events = await self._publish(
            {"events": events},
            "tool_finished",
            tool_name=tool_name,
            tool_call_id=call_id,
            ok=result.ok,
            changed=result.changed,
            error_code=result.error_code,
            duration_ms=round((self.clock() - tool_started_at) * 1000),
            elapsed_ms=elapsed_ms(),
        )
        if result.metadata.get("delegated_child_awaiting_approval"):
            child_run_id = str(result.metadata.get("child_run_id") or "")
            task_id = str(result.metadata.get("task_id") or "")
            reason = "A delegated child run is awaiting user approval."
            events = await self._publish(
                {"events": events},
                "run_stopped",
                code="delegated_child_awaiting_approval",
                reason=reason,
                child_run_id=child_run_id,
                task_id=task_id,
                elapsed_ms=elapsed_ms(),
            )
            return RunOutcome(
                status="stopped",
                output=None,
                messages=messages,
                events=events,
                steps=restored.steps,
                tool_calls=restored.calls,
                mode=prior.mode,
                stop_reason="delegated_child_awaiting_approval",
                error=reason,
                guard_snapshot=restored.snapshot(),
                usage=normalize_usage(prior.usage),
                active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
                context_snapshot=dict(prior.context_snapshot),
                artifact_refs=[dict(item) for item in prior.artifact_refs],
                transcript_delta=[dict(item) for item in resume_transcript_delta],
                verification_trace=[dict(item) for item in resume_verification_trace],
                acceptance_report=dict(prior.acceptance_report),
                completion_verification_attempts=prior.completion_verification_attempts,
            )
        timed_out = await timeout_outcome(events, messages)
        if timed_out is not None:
            return timed_out
        made_progress = bool(pending.get("batch_made_progress")) or result.made_progress
        seen = list(pending.get("seen_observations") or [])[-observation_limit:]
        seen_set = set(seen)
        fingerprint = self._observation_fingerprint(tool_name, result.content)
        if result.ok and fingerprint not in seen_set:
            made_progress = True
            seen.append(fingerprint)
            seen_set.add(fingerprint)

        remaining_calls = [
            ModelToolCall(
                id=str(raw.get("id") or ""),
                name=str(raw.get("name") or ""),
                arguments=dict(raw.get("arguments") or {}),
            )
            for raw in pending.get("remaining_calls") or []
        ]
        for remaining_index, call in enumerate(remaining_calls):
            timed_out = await timeout_outcome(events, messages)
            if timed_out is not None:
                return timed_out
            call_decision = restored.before_tool_call(call.name, call.arguments)
            if call_decision.stop:
                events = await self._publish(
                    {"events": events},
                    "run_stopped",
                    code=call_decision.code,
                    reason=call_decision.reason,
                )
                return RunOutcome(
                    status="stopped",
                    output=None,
                    messages=messages,
                    events=events,
                    steps=restored.steps,
                    tool_calls=restored.calls,
                    mode=prior.mode,
                    stop_reason=call_decision.code,
                    error=call_decision.reason,
                    guard_snapshot=restored.snapshot(),
                    usage=normalize_usage(prior.usage),
                    active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
                    context_snapshot=dict(prior.context_snapshot),
                    artifact_refs=[dict(item) for item in prior.artifact_refs],
                    transcript_delta=[dict(item) for item in resume_transcript_delta],
                    verification_trace=[dict(item) for item in resume_verification_trace],
                    acceptance_report=dict(prior.acceptance_report),
                    completion_verification_attempts=prior.completion_verification_attempts,
                )

            tool_started_at = self.clock()
            events = await self._publish(
                {"events": events},
                "tool_started",
                tool_name=call.name,
                tool_call_id=call.id,
                **safe_tool_argument_summary(call.name, call.arguments),
                elapsed_ms=elapsed_ms(),
            )
            remaining_result = await self.tool_registry.execute_async(
                call.name,
                call.arguments,
                approved=False,
                call_id=call.id,
            )
            if remaining_result.approval_required:
                events = await self._publish(
                    {"events": events},
                    "tool_finished",
                    tool_name=call.name,
                    tool_call_id=call.id,
                    ok=False,
                    changed=False,
                    error_code=remaining_result.error_code,
                    pending_approval=True,
                    duration_ms=round((self.clock() - tool_started_at) * 1000),
                    elapsed_ms=elapsed_ms(),
                )
                if remaining_result.approval_request is not None:
                    remaining_result.approval_request.id = call.id
                    next_pending = remaining_result.approval_request.to_dict()
                else:
                    next_pending = {"id": call.id, "tool_name": call.name, "arguments": call.arguments}
                next_pending["remaining_calls"] = [
                    {"id": item.id, "name": item.name, "arguments": item.arguments}
                    for item in remaining_calls[remaining_index + 1 :]
                ]
                next_pending["batch_made_progress"] = made_progress
                next_pending["seen_observations"] = seen[-observation_limit:]
                state = {"events": events}
                events = await self._publish(
                    state,
                    "approval_requested",
                    request=safe_approval_request_summary(next_pending),
                )
                timed_out = await timeout_outcome(events, messages)
                if timed_out is not None:
                    return timed_out
                return RunOutcome(
                    status="awaiting_approval",
                    output=None,
                    messages=messages,
                    events=events,
                    steps=restored.steps,
                    tool_calls=restored.calls,
                    mode=prior.mode,
                    pending_approval=next_pending,
                    guard_snapshot=restored.snapshot(),
                    usage=normalize_usage(prior.usage),
                    active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
                    context_snapshot=dict(prior.context_snapshot),
                    artifact_refs=[dict(item) for item in prior.artifact_refs],
                    transcript_delta=[dict(item) for item in resume_transcript_delta],
                    verification_trace=[dict(item) for item in resume_verification_trace],
                    acceptance_report=dict(prior.acceptance_report),
                    completion_verification_attempts=prior.completion_verification_attempts,
                )

            remaining_tool_message = {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": json.dumps(remaining_result.to_dict(), ensure_ascii=False),
            }
            messages.append(remaining_tool_message)
            resume_transcript_delta.append(dict(remaining_tool_message))
            resume_verification_trace.append(dict(remaining_tool_message))
            remaining_fingerprint = self._observation_fingerprint(call.name, remaining_result.content)
            observation_is_new = remaining_fingerprint not in seen_set
            if observation_is_new:
                seen.append(remaining_fingerprint)
                seen_set.add(remaining_fingerprint)
            made_progress = made_progress or remaining_result.changed or (remaining_result.ok and observation_is_new)
            state = {"events": events}
            events = await self._publish(
                state,
                "tool_finished",
                tool_name=call.name,
                tool_call_id=call.id,
                ok=remaining_result.ok,
                changed=remaining_result.changed,
                error_code=remaining_result.error_code,
                duration_ms=round((self.clock() - tool_started_at) * 1000),
                elapsed_ms=elapsed_ms(),
            )
            if remaining_result.metadata.get("delegated_child_awaiting_approval"):
                child_run_id = str(remaining_result.metadata.get("child_run_id") or "")
                task_id = str(remaining_result.metadata.get("task_id") or "")
                reason = "A delegated child run is awaiting user approval."
                events = await self._publish(
                    {"events": events},
                    "run_stopped",
                    code="delegated_child_awaiting_approval",
                    reason=reason,
                    child_run_id=child_run_id,
                    task_id=task_id,
                    elapsed_ms=elapsed_ms(),
                )
                return RunOutcome(
                    status="stopped",
                    output=None,
                    messages=messages,
                    events=events,
                    steps=restored.steps,
                    tool_calls=restored.calls,
                    mode=prior.mode,
                    stop_reason="delegated_child_awaiting_approval",
                    error=reason,
                    guard_snapshot=restored.snapshot(),
                    usage=normalize_usage(prior.usage),
                    active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
                    context_snapshot=dict(prior.context_snapshot),
                    artifact_refs=[dict(item) for item in prior.artifact_refs],
                    transcript_delta=[dict(item) for item in resume_transcript_delta],
                    verification_trace=[dict(item) for item in resume_verification_trace],
                    acceptance_report=dict(prior.acceptance_report),
                    completion_verification_attempts=prior.completion_verification_attempts,
                )
            if remaining_result.metadata.get("needs_user_input"):
                question = str(remaining_result.metadata.get("question") or remaining_result.content).strip()
                events = await self._publish(
                    {"events": events},
                    "user_question_requested",
                    tool_name=call.name,
                    tool_call_id=call.id,
                    question_chars=len(question),
                    requires_next_message=True,
                    elapsed_ms=elapsed_ms(),
                )
                output = f"我需要先确认：{question}"
                events = await self._publish(
                    {"events": events},
                    "run_stopped",
                    code="needs_user_input",
                    reason="Agent requires user input before completion",
                    elapsed_ms=elapsed_ms(),
                )
                return RunOutcome(
                    status="stopped",
                    output=output,
                    messages=messages,
                    events=events,
                    steps=restored.steps,
                    tool_calls=restored.calls,
                    mode=prior.mode,
                    stop_reason="needs_user_input",
                    guard_snapshot=restored.snapshot(),
                    usage=normalize_usage(prior.usage),
                    active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
                    context_snapshot=dict(prior.context_snapshot),
                    artifact_refs=[dict(item) for item in prior.artifact_refs],
                    transcript_delta=[dict(item) for item in resume_transcript_delta],
                    verification_trace=[dict(item) for item in resume_verification_trace],
                    acceptance_report=dict(prior.acceptance_report),
                    completion_verification_attempts=prior.completion_verification_attempts,
                )
            timed_out = await timeout_outcome(events, messages)
            if timed_out is not None:
                return timed_out

        progress_decision = restored.record_progress(made_progress)
        if progress_decision.stop:
            events = await self._publish(
                {"events": events},
                "run_stopped",
                code=progress_decision.code,
                reason=progress_decision.reason,
            )
            return RunOutcome(
                status="stopped",
                output=None,
                messages=messages,
                events=events,
                steps=restored.steps,
                tool_calls=restored.calls,
                mode=prior.mode,
                stop_reason=progress_decision.code,
                error=progress_decision.reason,
                guard_snapshot=restored.snapshot(),
                usage=normalize_usage(prior.usage),
                active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
                context_snapshot=dict(prior.context_snapshot),
                artifact_refs=[dict(item) for item in prior.artifact_refs],
                transcript_delta=[dict(item) for item in resume_transcript_delta],
                verification_trace=[dict(item) for item in resume_verification_trace],
                acceptance_report=dict(prior.acceptance_report),
                completion_verification_attempts=prior.completion_verification_attempts,
            )
        resumed_context = dict(runtime_context or {})
        return await self.run(
            system_prompt=str(resumed_context.get("system_prompt") or ""),
            agent_instructions=resumed_context.get("agent_instructions"),
            workspace_rules=resumed_context.get("workspace_rules"),
            summary=resumed_context.get("summary"),
            memories=resumed_context.get("memories", []),
            recent_messages=[],
            mode=prior.mode,
            thread_id=thread_id,
            prepared_messages=messages,
            prior_events=events,
            guard_snapshot=restored.snapshot(),
            prior_usage=prior.usage,
            prior_seen_observations=seen,
            prior_active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
            context_snapshot=prior.context_snapshot,
            permission_policy=resumed_context.get("permission_policy"),
            task_state=resumed_context.get("task_state"),
            session_id=resumed_context.get("session_id"),
            context_sequence=int(resumed_context.get("context_sequence") or 0),
            context_version=int(resumed_context.get("context_version") or 0),
            prompt_cache_key_seed=resumed_context.get("prompt_cache_key_seed"),
            artifact_refs=prior.artifact_refs,
            transcript_delta=resume_transcript_delta,
            prior_verification_trace=resume_verification_trace,
            prior_completion_verification_attempts=prior.completion_verification_attempts,
            prior_acceptance_report=prior.acceptance_report,
            task_anchor=(
                str(resumed_context.get("task_anchor") or "").strip()
                or self.context_manager.task_from_messages(prior.messages)
            ),
        )

    @staticmethod
    def _delegated_task_calls_from_messages(
        messages: Sequence[Mapping[str, Any]],
    ) -> list[tuple[int, str, dict[str, Any]]]:
        """Locate every parent ``task`` call still paused for a child.

        All parallel sibling results are persisted before a parent pauses.
        Continuation refreshes only observations whose structured metadata
        still says ``delegated_child_awaiting_approval``. Looking up the
        preceding assistant call prevents inventing arguments or replaying a
        different task from a later model turn.
        """

        paused: list[tuple[int, str, dict[str, Any]]] = []
        for tool_index, tool_message in enumerate(messages):
            tool_message = messages[tool_index]
            if tool_message.get("role") != "tool" or tool_message.get("name") != "task":
                continue
            try:
                tool_payload = json.loads(str(tool_message.get("content") or "{}"))
            except json.JSONDecodeError:
                continue
            metadata = tool_payload.get("metadata") if isinstance(tool_payload, Mapping) else None
            if not isinstance(metadata, Mapping) or not metadata.get("delegated_child_awaiting_approval"):
                continue
            call_id = str(tool_message.get("tool_call_id") or "").strip()
            if not call_id:
                continue
            for assistant_index in range(tool_index - 1, -1, -1):
                assistant_message = messages[assistant_index]
                if assistant_message.get("role") != "assistant":
                    continue
                for raw_call in assistant_message.get("tool_calls") or []:
                    if not isinstance(raw_call, Mapping):
                        continue
                    function = raw_call.get("function")
                    if not isinstance(function, Mapping):
                        continue
                    if str(raw_call.get("id") or "") != call_id or function.get("name") != "task":
                        continue
                    raw_arguments = function.get("arguments") or {}
                    if isinstance(raw_arguments, str):
                        try:
                            raw_arguments = json.loads(raw_arguments)
                        except json.JSONDecodeError as exc:
                            raise ValueError("delegated task arguments are not valid JSON") from exc
                    if not isinstance(raw_arguments, Mapping):
                        raise ValueError("delegated task arguments must be an object")
                    paused.append((tool_index, call_id, dict(raw_arguments)))
                    break
                else:
                    continue
                break
            else:
                raise ValueError("delegated task call has no matching assistant tool call")
        if not paused:
            raise ValueError("delegated child pause has no awaiting task tool result")
        return paused

    async def resume_after_delegated_child(
        self,
        prior: RunOutcome,
        *,
        thread_id: str | None = None,
        runtime_context: Mapping[str, Any] | None = None,
    ) -> RunOutcome:
        """Continue a parent after a delegated child reaches a terminal state.

        This is deliberately separate from :meth:`resume_after_approval`:
        the parent was not awaiting a user decision.  It reuses only the
        persisted idempotency key for the original ``task`` tool call, then
        lets the real parent model inspect that terminal child payload and
        decide whether to finish or take another safe step.
        """

        if (
            prior.status != "stopped"
            or prior.stop_reason != "delegated_child_awaiting_approval"
        ):
            raise ValueError("only a parent stopped for a delegated child can continue")
        if self.config.max_run_seconds is not None and self.config.max_run_seconds < 0:
            raise ValueError("max_run_seconds must be non-negative")
        if self.config.observation_history_limit < 1:
            raise ValueError("observation_history_limit must be positive")

        paused_calls = self._delegated_task_calls_from_messages(prior.messages)
        active_started_at = self.clock()
        active_elapsed_base = max(0.0, float(prior.active_elapsed_seconds or 0.0))
        restored = LoopGuard(
            max_steps=self.config.max_steps,
            max_calls=self.config.max_tool_calls,
            identical_limit=self.config.identical_call_limit,
            no_progress_limit=self.config.no_progress_limit,
        )
        restored.restore(prior.guard_snapshot)

        def elapsed_ms() -> int:
            return round((active_elapsed_base + max(0.0, self.clock() - active_started_at)) * 1000)

        time_decision = self._run_time_decision(active_elapsed_base, active_started_at)
        if time_decision.stop:
            events = await self._publish(
                {"events": list(prior.events)},
                "run_stopped",
                code=time_decision.code,
                reason=time_decision.reason,
            )
            return RunOutcome(
                status="stopped",
                output=None,
                messages=list(prior.messages),
                events=events,
                steps=restored.steps,
                tool_calls=restored.calls,
                mode=prior.mode,
                stop_reason=time_decision.code,
                error=time_decision.reason,
                guard_snapshot=restored.snapshot(),
                usage=normalize_usage(prior.usage),
                active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
                context_snapshot=dict(prior.context_snapshot),
                artifact_refs=[dict(item) for item in prior.artifact_refs],
                transcript_delta=prior.transcript_delta,
                verification_trace=[dict(item) for item in prior.verification_trace],
                acceptance_report=dict(prior.acceptance_report),
                completion_verification_attempts=prior.completion_verification_attempts,
            )

        messages = [dict(message) for message in prior.messages]
        events = list(prior.events)
        delegated_transcript_delta: list[dict[str, Any]] = []
        delegated_verification_trace = [dict(item) for item in prior.verification_trace]
        still_waiting: list[dict[str, str]] = []
        for tool_index, call_id, arguments in paused_calls:
            events = await self._publish(
                {"events": events},
                "delegated_child_continuation_started",
                tool_name="task",
                tool_call_id=call_id,
                elapsed_ms=elapsed_ms(),
            )
            tool_started_at = self.clock()
            # ``approved=True`` is safe here: it does not start new work. The
            # exact task already passed parent approval, and the idempotent
            # delegate returns the persisted child state for this call id.
            result = await self.tool_registry.execute_async(
                "task",
                arguments,
                approved=True,
                call_id=call_id,
            )
            events = await self._publish(
                {"events": events},
                "tool_finished",
                tool_name="task",
                tool_call_id=call_id,
                ok=result.ok,
                changed=result.changed,
                error_code=result.error_code,
                resumed_after_delegated_child=True,
                duration_ms=round((self.clock() - tool_started_at) * 1000),
                elapsed_ms=elapsed_ms(),
            )
            terminal_tool_message = {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "task",
                "content": json.dumps(result.to_dict(), ensure_ascii=False),
            }
            messages[tool_index] = terminal_tool_message
            delegated_transcript_delta.append(dict(terminal_tool_message))
            replaced_trace_result = False
            for trace_index, trace_message in enumerate(delegated_verification_trace):
                if (
                    trace_message.get("role") == "tool"
                    and str(trace_message.get("tool_call_id") or "") == call_id
                ):
                    delegated_verification_trace[trace_index] = dict(terminal_tool_message)
                    replaced_trace_result = True
                    break
            if not replaced_trace_result:
                delegated_verification_trace.append(dict(terminal_tool_message))
            if result.metadata.get("delegated_child_awaiting_approval"):
                still_waiting.append({
                    "child_run_id": str(result.metadata.get("child_run_id") or ""),
                    "task_id": str(result.metadata.get("task_id") or ""),
                    "tool_call_id": call_id,
                })

        if still_waiting:
            reason = "One or more delegated child runs are still awaiting user approval."
            events = await self._publish(
                {"events": events},
                "run_stopped",
                code="delegated_child_awaiting_approval",
                reason=reason,
                waiting_children=still_waiting,
                elapsed_ms=elapsed_ms(),
            )
            return RunOutcome(
                status="stopped",
                output=None,
                messages=messages,
                events=events,
                steps=restored.steps,
                tool_calls=restored.calls,
                mode=prior.mode,
                stop_reason="delegated_child_awaiting_approval",
                error=reason,
                guard_snapshot=restored.snapshot(),
                usage=normalize_usage(prior.usage),
                active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
                context_snapshot=dict(prior.context_snapshot),
                artifact_refs=[dict(item) for item in prior.artifact_refs],
                transcript_delta=delegated_transcript_delta,
                verification_trace=delegated_verification_trace,
                acceptance_report=dict(prior.acceptance_report),
                completion_verification_attempts=prior.completion_verification_attempts,
            )
        resumed_context = dict(runtime_context or {})
        return await self.run(
            system_prompt=str(resumed_context.get("system_prompt") or ""),
            agent_instructions=resumed_context.get("agent_instructions"),
            workspace_rules=resumed_context.get("workspace_rules"),
            summary=resumed_context.get("summary"),
            memories=resumed_context.get("memories", []),
            recent_messages=[],
            mode=prior.mode,
            thread_id=thread_id,
            prepared_messages=messages,
            prior_events=events,
            guard_snapshot=restored.snapshot(),
            prior_usage=prior.usage,
            prior_active_elapsed_seconds=self._active_elapsed(active_elapsed_base, active_started_at),
            context_snapshot=prior.context_snapshot,
            permission_policy=resumed_context.get("permission_policy"),
            task_state=resumed_context.get("task_state"),
            session_id=resumed_context.get("session_id"),
            context_sequence=int(resumed_context.get("context_sequence") or 0),
            context_version=int(resumed_context.get("context_version") or 0),
            prompt_cache_key_seed=resumed_context.get("prompt_cache_key_seed"),
            artifact_refs=prior.artifact_refs,
            transcript_delta=delegated_transcript_delta,
            prior_verification_trace=delegated_verification_trace,
            prior_completion_verification_attempts=prior.completion_verification_attempts,
            prior_acceptance_report=prior.acceptance_report,
            task_anchor=(
                str(resumed_context.get("task_anchor") or "").strip()
                or self.context_manager.task_from_messages(prior.messages)
            ),
        )
