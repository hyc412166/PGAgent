"""Persistence bridge between HTTP sessions and the AgentRuntime."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import case, or_, select

from app import database as database_module
from app.config import settings
from app.database import (
    Agent,
    AgentMessage,
    Approval,
    ChatMessage,
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    Memory,
    ModelConnection,
    Run,
    RunEvent,
    Session,
    Skill,
    TeamTask,
    UsageRecord,
    Workspace,
)
from app.runtime import AgentRuntime, ContextManager, RunOutcome, RuntimeConfig, normalize_usage
from app.runtime.context import estimate_tokens, message_tokens
from app.tools import create_default_registry
from app.tools.registry import TOOL_SCHEMAS
from app.tools.types import ToolResult

from .model_gateway import ModelConfigurationError, ProviderConfig, build_model_call
from .run_stream import run_stream_broker


ACTIVE_STATUSES = {"received", "preparing_context", "planning", "acting", "observing", "running"}

_DELEGATE_OUTPUT_LIMIT = 16_000
_DELEGATE_MESSAGE_LIMIT = 20_000
_DELEGATE_AGENT_CATALOG_LIMIT = 40
_DELEGATE_CHILD_SYSTEM_SUFFIX = (
    "\n\n你正在作为受限的子 Agent 执行一项已分配任务。"
    "只完成下方任务并返回可验证的结构化结论；不要再次委派任务，"
    "不要假称调用未启用的工具，也不要把问题直接抛给用户。"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_arguments(arguments: Any) -> str:
    return json.dumps(
        _json_safe(arguments or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _configuration_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_arguments(value).encode("utf-8")).hexdigest()


def approval_matches_pending(approval: Approval, pending: dict[str, Any] | None) -> bool:
    if not pending:
        return False
    return (
        approval.tool_name == str(pending.get("tool_name") or "")
        and _canonical_arguments(approval.arguments) == _canonical_arguments(pending.get("arguments"))
    )


def _message_payload(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_name:
        payload["name"] = message.tool_name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _conversation_token_count(summary: str, messages: list[dict[str, Any]]) -> int:
    summary_tokens = estimate_tokens(summary) if summary.strip() else 0
    return summary_tokens + sum(message_tokens(message) for message in messages)


def _prepare_session_history(db: Any, session: Session) -> list[dict[str, Any]]:
    """Return uncompacted history and compact it once the 90% threshold is hit."""

    query = select(ChatMessage).where(ChatMessage.session_id == session.id)
    if session.last_compacted_at is not None:
        query = query.where(ChatMessage.created_at > session.last_compacted_at)
    rows = list(db.scalars(query.order_by(ChatMessage.created_at.asc())))
    messages = [_message_payload(item) for item in rows]
    used_tokens = _conversation_token_count(session.context_summary or "", messages)

    if used_tokens >= settings.compact_threshold_tokens and rows:
        recent_budget = max(1, settings.compact_threshold_tokens // 2)
        keep_from = len(rows)
        kept_tokens = 0
        for index in range(len(rows) - 1, -1, -1):
            cost = message_tokens(messages[index])
            if kept_tokens and kept_tokens + cost > recent_budget:
                break
            if not kept_tokens and cost > recent_budget:
                # Preserve the newest user turn verbatim; ContextManager will
                # safely trim it for the provider if one message exceeds 100k.
                keep_from = index
                kept_tokens = cost
                break
            keep_from = index
            kept_tokens += cost

        # last_compacted_at is the only durable cursor. Never split rows that
        # share its timestamp, otherwise an un-compacted sibling could be lost
        # by the next `created_at > cursor` query.
        while (
            0 < keep_from < len(rows)
            and rows[keep_from - 1].created_at == rows[keep_from].created_at
        ):
            keep_from -= 1

        compacted_rows = rows[:keep_from]
        compacted_messages = messages[:keep_from]
        if compacted_rows:
            summary_source: list[dict[str, Any]] = []
            if session.context_summary:
                summary_source.append({"role": "summary", "content": session.context_summary})
            summary_source.extend(compacted_messages)
            session.context_summary = ContextManager(max_tokens=settings.context_limit_tokens).compact_messages(
                summary_source,
                token_budget=min(12_000, settings.compact_threshold_tokens // 4),
            )
            session.last_compacted_at = compacted_rows[-1].created_at
            rows = rows[keep_from:]
            messages = messages[keep_from:]
            used_tokens = _conversation_token_count(session.context_summary, messages)

    # This is the durable conversation footprint. The provider-facing builder
    # separately guarantees it never emits more than context_limit_tokens.
    session.context_tokens = min(used_tokens, settings.context_limit_tokens)
    return messages


def _explicit_setting(*values: str | None) -> str | None:
    for value in values:
        normalized = (value or "").strip()
        if normalized and normalized != "auto":
            return normalized
    return None


def _fallback_connection(db: Any, *, provider: str | None = None) -> ModelConnection | None:
    query = select(ModelConnection).where(ModelConnection.enabled.is_(True))
    if provider:
        query = query.where(ModelConnection.provider == provider)
    query = query.order_by(
        case(
            (ModelConnection.status.in_(("connected", "healthy", "online")), 0),
            (ModelConnection.last_checked_at.is_not(None), 1),
            else_=2,
        ),
        ModelConnection.last_checked_at.desc(),
        ModelConnection.created_at.asc(),
    )
    return db.scalar(query)


def _effective_connection(db: Any, session: Session | None, agent: Agent) -> ModelConnection | None:
    candidate_ids = [session.model_connection_id if session else None, agent.model_connection_id]
    for connection_id in dict.fromkeys(item for item in candidate_ids if item):
        connection = db.get(ModelConnection, connection_id)
        if connection is not None and connection.enabled:
            return connection
    return _fallback_connection(db)


def _allowed_runtime_tool_names(tool_ids: list[str]) -> list[str]:
    """Map persisted catalog IDs to only tools this runtime actually implements."""

    selected: list[str] = []
    seen: set[str] = set()
    for raw_id in tool_ids:
        tool_name = str(raw_id or "").strip()
        if tool_name in TOOL_SCHEMAS and tool_name not in seen:
            selected.append(tool_name)
            seen.add(tool_name)
    return selected


def _read_selected_skill_instructions(db: Any, skill_ids: list[str]) -> list[dict[str, str]]:
    """Read only selected, PGAgent-managed ``SKILL.md`` text into a run binding.

    A Skill remains inert: this helper neither imports Python nor executes a
    script.  It skips a missing/tampered package instead of sending a path or
    filesystem exception to the model.
    """

    requested = [str(item).strip() for item in skill_ids if str(item).strip()]
    if not requested:
        return []
    rows = list(db.scalars(select(Skill).where(Skill.id.in_(requested), Skill.enabled.is_(True))))
    by_id = {item.id: item for item in rows}
    managed_root = (settings.data_dir / "skills").resolve()
    items: list[dict[str, str]] = []
    for skill_id in requested:
        skill = by_id.get(skill_id)
        if skill is None:
            continue
        try:
            root = Path(skill.root_path).resolve(strict=True)
            root.relative_to(managed_root)
            skill_file = (root / "SKILL.md").resolve(strict=True)
            skill_file.relative_to(root)
            if not skill_file.is_file() or skill_file.stat().st_size > 2_000_000:
                continue
            content = skill_file.read_text(encoding="utf-8")[:40_000]
        except (OSError, UnicodeError, ValueError):
            continue
        if not content.strip():
            continue
        items.append(
            {
                "id": skill.id,
                "slug": skill.slug,
                "name": skill.name,
                "description": skill.description[:1_000],
                "content": content,
            }
        )
    return items


def _session_todo_state(db: Any, session_id: str | None) -> list[dict[str, Any]]:
    """Carry the last durable todo snapshot into a follow-up session run."""

    if not session_id:
        return []
    event = db.scalar(
        select(RunEvent)
        .join(Run, RunEvent.run_id == Run.id)
        .where(Run.session_id == session_id, RunEvent.event_type == "runtime_snapshot")
        .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
    )
    if event is None or not isinstance(event.payload, dict):
        return []
    binding = event.payload.get("runtime_binding")
    todos = binding.get("todo_state") if isinstance(binding, dict) else None
    return list(todos) if isinstance(todos, list) else []


def _single_line(value: object, *, limit: int) -> str:
    """Render user-configured metadata safely inside a system capability hint."""

    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


def _active_child_agents(db: Any) -> list[Agent]:
    """Return only explicitly user-created, currently enabled delegate targets."""

    return list(db.scalars(
        select(Agent)
        .where(
            Agent.id != DEFAULT_AGENT_ID,
            Agent.is_default.is_(False),
            Agent.enabled.is_(True),
        )
        .order_by(Agent.updated_at.desc(), Agent.id.asc())
    ))


def _delegate_catalog_prompt(db: Any) -> str:
    """Make the main runtime's valid delegate IDs visible to the model.

    The main coordinator stays system-owned and prompt-free as a profile. This
    is dynamic tool metadata, not a user-authored replacement system prompt.
    It deliberately exposes only names/descriptions/IDs—not child prompts,
    credentials, workspaces, or capability internals.
    """

    children = _active_child_agents(db)[:_DELEGATE_AGENT_CATALOG_LIMIT]
    if not children:
        return ""
    lines = [
        "可委派的子 Agent（仅在任务确实较复杂、专业，或用户明确要求时使用 task 工具）：",
        "- task 必须传入下列精确 agent_id；子 Agent 的实际权限和工具会由系统再次校验。",
    ]
    for child in children:
        name = _single_line(child.name, limit=120) or "未命名子 Agent"
        description = _single_line(child.description, limit=300)
        suffix = f"：{description}" if description else ""
        lines.append(f"- {child.id} | {name}{suffix}")
    if len(children) >= _DELEGATE_AGENT_CATALOG_LIMIT:
        lines.append("- 列表已截断；如未找到匹配子 Agent，请直接完成可安全完成的部分或向用户说明。")
    return "\n".join(lines)


def _model_id_for_delegate(
    connection: ModelConnection,
    *,
    preferred: str | None,
    inherited_model_id: str | None,
    may_inherit_model: bool,
) -> str | None:
    """Resolve a child model without accidentally crossing connections."""

    selected = str(preferred or "").strip()
    if selected:
        return selected[:255]
    selected = str(connection.default_model or "").strip()
    if selected:
        return selected[:255]
    candidates = [*(connection.discovered_models or []), *(connection.manual_models or [])]
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized[:255]
    if may_inherit_model:
        selected = str(inherited_model_id or "").strip()
        if selected:
            return selected[:255]
    return None


def _delegate_result_content(payload: dict[str, Any]) -> str:
    """Bound the child response before it becomes a parent tool observation."""

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= _DELEGATE_MESSAGE_LIMIT:
        return encoded
    # Keep the structured envelope valid and signal truncation explicitly.
    compact = dict(payload)
    output = str(compact.get("output") or "")
    compact["output"] = output[: max(0, _DELEGATE_OUTPUT_LIMIT // 2)]
    compact["output_truncated"] = True
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)[:_DELEGATE_MESSAGE_LIMIT]


class _SubagentTaskDelegate:
    """One parent runtime's bounded bridge to a user-created child Agent.

    The delegate is intentionally not a queue that can spawn arbitrary work:
    one ``task`` call produces at most one persisted TeamTask and one child
    Run.  The child uses the parent run's already-frozen workspace and
    permission mode, an intersected tool allowlist, and never receives the
    ``task`` tool.  It therefore cannot recursively create an unbounded agent
    tree or escape the user's active conversation policy.
    """

    def __init__(
        self,
        *,
        parent_run_id: str,
        parent_agent_id: str,
        parent_binding: dict[str, Any],
        parent_allowed_tool_names: list[str],
        permission_mode: str,
    ) -> None:
        self.parent_run_id = parent_run_id
        self.parent_agent_id = parent_agent_id
        self.parent_binding = dict(parent_binding)
        self.parent_allowed_tool_names = tuple(
            str(name).strip() for name in parent_allowed_tool_names if str(name).strip()
        )
        self.permission_mode = str(permission_mode or "smart")

    @staticmethod
    def _configuration_matches_parent(connection: ModelConnection, binding: dict[str, Any]) -> bool:
        return (
            connection.id == str(binding.get("model_connection_id") or "")
            and connection.enabled
            and connection.provider == str(binding.get("provider") or "")
            and connection.base_url == str(binding.get("base_url") or "")
            and connection.secret_ref == str(binding.get("secret_ref") or "")
            and _configuration_digest(connection.custom_headers or {})
            == str(binding.get("custom_headers_digest") or "")
        )

    def _task_idempotency_key(self, *, call_id: str | None, task: str, agent_id: str) -> str:
        if call_id:
            call_digest = hashlib.sha256(str(call_id).encode("utf-8")).hexdigest()[:32]
            return f"delegate:{self.parent_run_id}:call:{call_digest}"
        digest = hashlib.sha256(f"{agent_id}\0{task}".encode("utf-8")).hexdigest()[:32]
        return f"delegate:{self.parent_run_id}:{digest}"

    @staticmethod
    def _task_title(task: str) -> str:
        compact = _single_line(task, limit=200)
        return compact or "子 Agent 任务"

    @staticmethod
    def _safe_pending_summary(pending: dict[str, Any] | None) -> dict[str, Any] | None:
        if not pending:
            return None
        return {
            "tool_name": _single_line(pending.get("tool_name"), limit=100) or "unknown",
            "reason": _single_line(pending.get("reason"), limit=500),
            "requires_user_approval": True,
        }

    @staticmethod
    def _changed_workspace(outcome: RunOutcome) -> bool:
        return any(
            event.get("type") == "tool_finished" and bool(event.get("changed"))
            for event in outcome.events
            if isinstance(event, dict)
        )

    @staticmethod
    def _error_code_for_status(status: str) -> str | None:
        return {
            "awaiting_approval": "delegate_child_awaiting_approval",
            "stopped": "delegate_child_stopped",
            "failed": "delegate_child_failed",
        }.get(status, "delegate_child_incomplete") if status != "completed" else None

    def _tool_result_from_payload(self, payload: dict[str, Any]) -> ToolResult:
        status = str(payload.get("status") or "failed")
        return ToolResult(
            "task",
            status == "completed",
            _delegate_result_content(payload),
            changed=bool(payload.get("workspace_changed")),
            error_code=self._error_code_for_status(status),
            metadata={
                "task_id": str(payload.get("task_id") or ""),
                "child_run_id": str(payload.get("child_run_id") or ""),
                "child_agent_id": str(payload.get("agent", {}).get("id") or "")
                if isinstance(payload.get("agent"), dict)
                else "",
                "status": status,
                # A child approval is not an ordinary failed observation.  The
                # parent runtime uses this explicit marker to stop without
                # inventing a final answer while the child remains resumable.
                "delegated_child_awaiting_approval": status == "awaiting_approval",
            },
        )

    def _blocked_result(
        self,
        *,
        task_id: str | None,
        child_run_id: str | None,
        agent: Agent | None,
        code: str,
        message: str,
    ) -> ToolResult:
        payload = {
            "task_id": task_id,
            "child_run_id": child_run_id,
            "agent": {
                "id": agent.id if agent is not None else "",
                "name": _single_line(agent.name, limit=120) if agent is not None else "",
            },
            "status": "blocked",
            "error_code": code,
            "error": _single_line(message, limit=1_000),
        }
        return ToolResult(
            "task",
            False,
            _delegate_result_content(payload),
            error_code=code,
            metadata={"task_id": task_id or "", "child_run_id": child_run_id or "", "status": "blocked"},
        )

    def _persist_blocked_task(
        self,
        *,
        task_id: str,
        child_run_id: str | None,
        agent: Agent,
        code: str,
        message: str,
    ) -> None:
        payload = {
            "task_id": task_id,
            "child_run_id": child_run_id,
            "agent": {"id": agent.id, "name": _single_line(agent.name, limit=120)},
            "status": "blocked",
            "error_code": code,
            "error": _single_line(message, limit=1_000),
        }
        with database_module.SessionLocal() as db:
            task = db.get(TeamTask, task_id)
            if task is None:
                return
            task.status = "blocked"
            task.result = payload
            task.lease_owner = None
            task.lease_expires_at = None
            task.version += 1
            db.add(AgentMessage(
                team_id=task.team_id,
                task_id=task.id,
                sender_agent_id=agent.id,
                recipient_agent_id=self.parent_agent_id,
                message_type="BLOCKED",
                payload=payload,
                idempotency_key=f"delegate-result:{task.id}",
                hop_count=1,
            ))
            db.commit()

    def _remaining_parent_run_seconds(self, db: Any) -> float | None:
        """Return the child budget left in its parent run, if bounded.

        A delegated child executes inside the parent's active tool call, so
        wall-clock time since the parent run began is deliberately a
        conservative upper bound.  We never grant a second arbitrary timeout
        to a child: an unbounded parent stays unbounded and a bounded parent
        gives the child only its remaining allowance.
        """

        configured = settings.max_run_seconds
        if not configured:
            return None
        limit = max(0.0, float(configured))
        parent = db.get(Run, self.parent_run_id)
        if parent is None:
            raise RuntimeError("Parent run no longer exists")
        started_at = parent.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = max(0.0, (_utcnow() - started_at).total_seconds())
        return max(0.0, limit - elapsed)

    def _freeze_child_binding(
        self,
        db: Any,
        child: Agent,
    ) -> tuple[ProviderConfig, dict[str, Any], list[dict[str, str]]]:
        """Snapshot all execution-relevant child settings before model I/O."""

        parent_connection_id = str(self.parent_binding.get("model_connection_id") or "")
        requested_connection_id = str(child.model_connection_id or "").strip() or parent_connection_id
        connection = db.get(ModelConnection, requested_connection_id)
        if connection is None or not connection.enabled:
            raise ModelConfigurationError("子 Agent 的模型连接不存在或已禁用")
        inherits_parent_connection = connection.id == parent_connection_id
        if inherits_parent_connection and not self._configuration_matches_parent(connection, self.parent_binding):
            raise RuntimeError("主会话冻结的模型连接已变化，拒绝在变化配置中启动子 Agent")

        model_id = _model_id_for_delegate(
            connection,
            preferred=child.model_id,
            inherited_model_id=str(self.parent_binding.get("model_id") or ""),
            may_inherit_model=inherits_parent_connection,
        )
        if not model_id:
            raise ModelConfigurationError("子 Agent 没有可用模型，请为其配置模型或主会话模型")
        thinking_level = _explicit_setting(
            child.thinking_level,
            connection.thinking_level,
            str(self.parent_binding.get("thinking_level") or ""),
        ) or "auto"

        # A child can only receive the capability intersection. The primary
        # coordinator normally has the full catalog, but this remains safe for
        # standalone/future restricted parent runtimes as well.
        parent_allowed = set(self.parent_allowed_tool_names)
        child_tools = [
            tool_name
            for tool_name in _allowed_runtime_tool_names(list(getattr(child, "tool_ids", []) or []))
            if tool_name in parent_allowed and tool_name != "task"
        ]
        child_skill_ids = list(getattr(child, "skill_ids", []) or [])
        skill_instructions = _read_selected_skill_instructions(db, child_skill_ids)
        workspace_root = str(self.parent_binding.get("workspace_root") or "").strip()
        if not workspace_root:
            raise RuntimeError("主会话缺少冻结的工作区")

        provider_config = ProviderConfig(
            provider=connection.provider,
            base_url=connection.base_url,
            secret_ref=connection.secret_ref,
            model_id=model_id,
            model_connection_id=connection.id,
            thinking_level=thinking_level,
            custom_headers=dict(connection.custom_headers or {}),
        )
        binding = {
            "delegation_version": 1,
            "parent_run_id": self.parent_run_id,
            "agent_id": child.id,
            "agent_system_prompt": (str(child.system_prompt or "") + _DELEGATE_CHILD_SYSTEM_SUFFIX).strip(),
            "child_agent_id": child.id,
            "workspace_root": workspace_root,
            "model_connection_id": connection.id,
            "provider": connection.provider,
            "base_url": connection.base_url,
            "secret_ref": connection.secret_ref,
            "model_id": model_id,
            "thinking_level": thinking_level,
            "permission_mode": self.permission_mode,
            "tool_ids": list(getattr(child, "tool_ids", []) or []),
            "allowed_tool_names": child_tools,
            "skill_ids": child_skill_ids,
            "skill_instructions": skill_instructions,
            "todo_state": [],
            "custom_headers_digest": _configuration_digest(connection.custom_headers or {}),
        }
        return provider_config, binding, skill_instructions

    @staticmethod
    def _public_binding(binding: dict[str, Any]) -> dict[str, Any]:
        """Persist displayable evidence without leaking routes or credentials."""

        return {
            "model_connection_id": str(binding.get("model_connection_id") or ""),
            "provider": str(binding.get("provider") or ""),
            "model_id": str(binding.get("model_id") or ""),
            "thinking_level": str(binding.get("thinking_level") or "auto"),
            "permission_mode": str(binding.get("permission_mode") or "smart"),
            "allowed_tool_names": list(binding.get("allowed_tool_names") or []),
            "skill_ids": list(binding.get("skill_ids") or []),
            "workspace_inherited": True,
            "recursive_task_enabled": False,
        }

    async def __call__(self, task: str, *, agent_id: str = "", call_id: str | None = None) -> ToolResult:
        """Persist, execute and summarize exactly one bounded child task."""

        requested_agent_id = str(agent_id or "").strip()
        idempotency_key = self._task_idempotency_key(call_id=call_id, task=task, agent_id=requested_agent_id)
        child: Agent | None = None
        team_task_id: str | None = None
        child_run_id: str | None = None
        provider_config: ProviderConfig | None = None
        child_binding: dict[str, Any] | None = None
        child_skill_instructions: list[dict[str, str]] = []
        child_max_run_seconds: float | None = None
        with database_module.SessionLocal() as db:
            existing = db.scalar(select(TeamTask).where(TeamTask.idempotency_key == idempotency_key))
            if existing is not None:
                result = dict(existing.result or {})
                if result:
                    return self._tool_result_from_payload(result)
                return self._blocked_result(
                    task_id=existing.id,
                    child_run_id=None,
                    agent=None,
                    code="delegate_task_in_progress",
                    message="相同的子 Agent 任务已经在执行中",
                )

            child = db.get(Agent, requested_agent_id)
            if child is None or child.id == DEFAULT_AGENT_ID or child.is_default:
                return self._blocked_result(
                    task_id=None,
                    child_run_id=None,
                    agent=None,
                    code="delegate_agent_not_found",
                    message="请求的子 Agent 不存在或不是可委派的用户 Agent",
                )
            if not child.enabled:
                return self._blocked_result(
                    task_id=None,
                    child_run_id=None,
                    agent=child,
                    code="delegate_agent_disabled",
                    message="请求的子 Agent 已被禁用",
                )
            try:
                provider_config, child_binding, child_skill_instructions = self._freeze_child_binding(db, child)
                child_max_run_seconds = self._remaining_parent_run_seconds(db)
            except (ModelConfigurationError, RuntimeError) as exc:
                return self._blocked_result(
                    task_id=None,
                    child_run_id=None,
                    agent=child,
                    code="delegate_configuration_invalid",
                    message=str(exc),
                )

            if child_max_run_seconds is not None and child_max_run_seconds <= 0:
                return self._blocked_result(
                    task_id=None,
                    child_run_id=None,
                    agent=child,
                    code="delegate_parent_time_budget_exhausted",
                    message="The parent run has no remaining execution time for a delegated child.",
                )

            lease_owner = f"run:{self.parent_run_id}"
            team_task = TeamTask(
                team_id=lease_owner,
                title=self._task_title(task),
                description=task,
                priority="normal",
                status="in_progress",
                assignee_agent_id=child.id,
                lease_owner=lease_owner,
                lease_expires_at=(
                    _utcnow() + timedelta(seconds=child_max_run_seconds)
                    if child_max_run_seconds is not None
                    else None
                ),
                idempotency_key=idempotency_key,
                result={
                    "status": "in_progress",
                    "agent": {"id": child.id, "name": _single_line(child.name, limit=120)},
                    "binding": self._public_binding(child_binding),
                },
            )
            child_run = Run(
                # A child is attached to the parent session solely so the
                # current conversation can discover its approval card and
                # terminal result.  Its frozen delegation binding still
                # selects the child Agent during resume.
                session_id=db.scalar(select(Run.session_id).where(Run.id == self.parent_run_id)),
                workspace_id=None,
                agent_id=child.id,
                mode="auto",
                status="received",
            )
            db.add_all([team_task, child_run])
            db.flush()
            child_run.workspace_id = db.scalar(
                select(Run.workspace_id).where(Run.id == self.parent_run_id)
            )
            child_binding = {
                **child_binding,
                "team_task_id": team_task.id,
                "parent_agent_id": self.parent_agent_id,
                "parent_session_id": child_run.session_id,
                "max_run_seconds": child_max_run_seconds,
            }
            team_task.result = {
                **dict(team_task.result or {}),
                "task_id": team_task.id,
                "child_run_id": child_run.id,
                "parent_run_id": self.parent_run_id,
                "parent_session_id": child_run.session_id,
            }
            # This link exists before model I/O.  It lets terminal failure
            # handling reconcile the TeamTask even if no runtime snapshot was
            # ever produced.
            db.add(RunEvent(
                run_id=child_run.id,
                event_type="delegation_link",
                payload={
                    "team_task_id": team_task.id,
                    "parent_run_id": self.parent_run_id,
                    "parent_agent_id": self.parent_agent_id,
                    "parent_session_id": child_run.session_id,
                },
            ))
            # The parent frozen root is the authority even if a user edits a
            # child profile's workspace while this delegate runs.
            db.add(AgentMessage(
                team_id=team_task.team_id,
                task_id=team_task.id,
                sender_agent_id=self.parent_agent_id,
                recipient_agent_id=child.id,
                message_type="TASK_ASSIGNED",
                payload={
                    "task": task,
                    "parent_run_id": self.parent_run_id,
                    "child_run_id": child_run.id,
                    "parent_session_id": child_run.session_id,
                    "binding": self._public_binding(child_binding),
                },
                idempotency_key=f"delegate-assignment:{team_task.id}",
                hop_count=1,
            ))
            db.commit()
            team_task_id = team_task.id
            child_run_id = child_run.id

        assert child is not None and provider_config is not None and child_binding is not None
        assert team_task_id is not None and child_run_id is not None
        child_registry = create_default_registry(
            str(child_binding["workspace_root"]),
            allowed_tool_names=child_binding["allowed_tool_names"],
            permission_mode=child_binding["permission_mode"],
            skill_instructions=child_skill_instructions,
            todo_state=[],
            # Deliberately omit task_delegate: task was removed from the
            # allowlist and a child never obtains a recursive dispatch hook.
        )
        child_runtime = AgentRuntime(
            model_call=build_model_call(provider_config),
            tool_registry=child_registry,
            context_manager=ContextManager(max_tokens=settings.context_limit_tokens),
            event_sink=RunCoordinator._event_sink(child_run_id),
            config=RuntimeConfig(
                max_steps=settings.max_steps,
                max_tool_calls=settings.max_tool_calls,
                identical_call_limit=settings.max_identical_calls,
                no_progress_limit=settings.no_progress_limit,
                model_timeout_seconds=min(
                    float(settings.model_timeout_seconds),
                    child_max_run_seconds,
                ) if child_max_run_seconds is not None else settings.model_timeout_seconds,
                max_run_seconds=child_max_run_seconds,
            ),
            checkpointer=coordinator.checkpointer,
        )
        try:
            outcome = await child_runtime.run(
                system_prompt=str(child_binding["agent_system_prompt"]),
                agent_instructions="",
                workspace_rules=f"只能访问主会话冻结的工作区：{child_binding['workspace_root']}",
                summary="",
                memories=[],
                recent_messages=[{"role": "user", "content": task}],
                mode="auto",
                thread_id=child_run_id,
            )
        except asyncio.CancelledError:
            RunCoordinator._persist_failure(child_run_id, RuntimeError("Delegated child execution was cancelled"))
            raise
        except Exception as exc:
            RunCoordinator._persist_failure(child_run_id, exc)
            return self._blocked_result(
                task_id=team_task_id,
                child_run_id=child_run_id,
                agent=child,
                code="delegate_execution_error",
                message=str(exc) or type(exc).__name__,
            )

        outcome.runtime_binding = {
            **child_binding,
            **child_registry.runtime_state(),
        }
        RunCoordinator._persist_outcome(child_run_id, outcome)
        with database_module.SessionLocal() as db:
            persisted_task = db.get(TeamTask, team_task_id)
            if persisted_task is None:
                return self._blocked_result(
                    task_id=team_task_id,
                    child_run_id=child_run_id,
                    agent=child,
                    code="delegate_task_missing",
                    message="Delegated task record disappeared before its outcome could be read.",
                )
            payload = dict(persisted_task.result or {})
        return self._tool_result_from_payload(payload)


class RunCoordinator:
    """Launch, persist and resume local runs without blocking HTTP requests."""

    def __init__(self) -> None:
        self.checkpointer: Any | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._queued_resumes: set[str] = set()
        self._shutting_down = False

    def set_checkpointer(self, checkpointer: Any | None) -> None:
        self.checkpointer = checkpointer
        if checkpointer is not None:
            self._shutting_down = False

    def _clear_task(self, completed: asyncio.Task[None], run_id: str) -> None:
        if self._tasks.get(run_id) is completed:
            self._tasks.pop(run_id, None)

    def _launch_queued_resume(self, run_id: str) -> None:
        if run_id not in self._queued_resumes:
            return
        self._queued_resumes.discard(run_id)
        if not self._shutting_down:
            self.launch(run_id, resume=True)

    def launch(self, run_id: str, *, resume: bool = False) -> bool:
        if self._shutting_down:
            return False
        current = self._tasks.get(run_id)
        if current and not current.done():
            if not resume:
                return False
            if run_id not in self._queued_resumes:
                self._queued_resumes.add(run_id)
                loop = asyncio.get_running_loop()
                current.add_done_callback(
                    lambda _completed, key=run_id: loop.call_soon(self._launch_queued_resume, key)
                )
            return True
        task = asyncio.create_task(self._resume(run_id) if resume else self._execute(run_id), name=f"pgagent-run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda completed, key=run_id: self._clear_task(completed, key))
        return True

    def launch_delegated_child_continuation(self, run_id: str) -> bool:
        """Schedule the parent continuation created by a terminal child.

        The parent row has already moved from its explicit child-wait stop to
        ``received`` in the child outcome transaction.  This method only owns
        in-process scheduling and never changes persistence, so callers can
        safely retry it without creating a second continuation task.
        """

        if self._shutting_down:
            return False
        current = self._tasks.get(run_id)
        if current is not None and not current.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # A synchronous maintenance caller can still leave durable state
            # for an explicit resume; do not create an orphan coroutine.
            return False
        task = loop.create_task(
            self._resume_delegated_child(run_id),
            name=f"pgagent-child-continuation-{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda completed, key=run_id: self._clear_task(completed, key))
        return True

    def launch_parent_continuation_if_queued(self, parent_event: dict[str, Any] | None) -> bool:
        """Start a durable queued parent continuation exactly once per event."""

        if not parent_event or not parent_event.get("continuation_queued"):
            return False
        parent_run_id = str(parent_event.get("parent_run_id") or "").strip()
        return bool(parent_run_id) and self.launch_delegated_child_continuation(parent_run_id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        self._queued_resumes.clear()
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    @staticmethod
    def reconcile_interrupted_runs() -> list[str]:
        """Make crashes visible instead of leaving runs permanently active."""

        parent_bridge_events: list[dict[str, Any]] = []
        parent_continuation_run_ids: list[str] = []
        with database_module.SessionLocal() as db:
            runs = list(db.scalars(select(Run).where(Run.status.in_(ACTIVE_STATUSES))))
            for run in runs:
                run.status = "stopped"
                run.stop_reason = "interrupted_restart"
                run.error_message = "PGAgent 在运行期间被关闭，可重新发送消息继续任务"
                run.finished_at = _utcnow()
                db.add(RunEvent(run_id=run.id, event_type="run_interrupted", payload={"reason": "process_restart"}))
                parent_event = RunCoordinator._sync_delegated_child_state(
                    db,
                    run,
                    status="stopped",
                    stop_reason="interrupted_restart",
                    error=run.error_message,
                    error_code="interrupted_restart",
                )
                if parent_event is not None:
                    parent_bridge_events.append(parent_event)
                    if parent_event.get("continuation_queued"):
                        parent_run_id = str(parent_event.get("parent_run_id") or "").strip()
                        if parent_run_id:
                            parent_continuation_run_ids.append(parent_run_id)
            db.commit()
        for parent_event in parent_bridge_events:
            parent_run_id = str(parent_event.get("parent_run_id") or "")
            if parent_run_id:
                run_stream_broker.publish(parent_run_id, parent_event)
        return list(dict.fromkeys(parent_continuation_run_ids))

    @staticmethod
    def _event_sink(run_id: str):
        status_by_event = {
            "context_prepared": "preparing_context",
            "context_resumed": "acting",
            "model_step_started": "acting",
            "tool_finished": "observing",
        }
        deferred_stream_events = {"approval_requested", "run_completed", "run_stopped", "model_failed"}

        def sink(event: dict[str, Any]) -> None:
            event_type = str(event.get("type") or "runtime_event")
            payload = {key: _json_safe(value) for key, value in event.items() if key != "type"}
            with database_module.SessionLocal() as db:
                run = db.get(Run, run_id)
                if run is None:
                    return
                db.add(RunEvent(run_id=run_id, event_type=event_type, step=payload.get("step"), payload=payload))
                if event_type in status_by_event:
                    run.status = status_by_event[event_type]
                db.commit()
            # Approval and terminal events are published only after the outcome
            # transaction makes their corresponding rows/messages visible.
            if event_type not in deferred_stream_events:
                run_stream_broker.publish(run_id, {"type": event_type, **payload})

        return sink

    @staticmethod
    def _stream_sink(run_id: str):
        def sink(event: dict[str, Any]) -> None:
            run_stream_broker.publish(run_id, _json_safe(event))

        return sink

    @staticmethod
    def _runtime_snapshot(outcome: RunOutcome) -> dict[str, Any]:
        return _json_safe({
            "status": outcome.status,
            "output": outcome.output,
            "messages": outcome.messages,
            "events": outcome.events,
            "steps": outcome.steps,
            "tool_calls": outcome.tool_calls,
            "mode": outcome.mode,
            "stop_reason": outcome.stop_reason,
            "error": outcome.error,
            "pending_approval": outcome.pending_approval,
            "guard_snapshot": outcome.guard_snapshot,
            "usage": outcome.usage,
            "active_elapsed_seconds": outcome.active_elapsed_seconds,
            "runtime_binding": outcome.runtime_binding,
        })

    @staticmethod
    def _outcome_from_snapshot(payload: dict[str, Any]) -> RunOutcome:
        return RunOutcome(
            status=str(payload.get("status") or "failed"),
            output=payload.get("output"),
            messages=list(payload.get("messages") or []),
            events=list(payload.get("events") or []),
            steps=int(payload.get("steps") or 0),
            tool_calls=int(payload.get("tool_calls") or 0),
            mode=str(payload.get("mode") or "auto"),
            stop_reason=payload.get("stop_reason"),
            error=payload.get("error"),
            pending_approval=payload.get("pending_approval"),
            guard_snapshot=dict(payload.get("guard_snapshot") or {}),
            usage=normalize_usage(payload.get("usage")),
            active_elapsed_seconds=max(0.0, float(payload.get("active_elapsed_seconds") or 0.0)),
            runtime_binding=dict(payload.get("runtime_binding") or {}),
        )

    @staticmethod
    def _resolve_runtime(
        run_id: str,
        *,
        runtime_binding: dict[str, Any] | None = None,
    ) -> tuple[AgentRuntime, dict[str, Any]]:
        with database_module.SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None:
                raise LookupError("运行不存在")
            session = db.get(Session, run.session_id) if run.session_id else None
            frozen_binding = dict(runtime_binding or {})
            delegated_child = bool(frozen_binding.get("delegation_version"))
            runtime_max_run_seconds: float | None = settings.max_run_seconds
            # Session runs always use the fixed PGAgent coordinator. Keep
            # standalone runs backwards compatible with their explicit Agent.
            # A delegated child is intentionally linked to its parent session
            # for approval visibility, but its frozen binding remains the
            # authority for agent identity on approval resume.
            agent_id = (
                str(frozen_binding.get("agent_id") or run.agent_id or DEFAULT_AGENT_ID)
                if delegated_child
                else (DEFAULT_AGENT_ID if session is not None else (run.agent_id or DEFAULT_AGENT_ID))
            )
            agent = db.get(Agent, agent_id) or db.get(Agent, DEFAULT_AGENT_ID)
            if agent is None:
                raise ModelConfigurationError("当前会话没有选择 Agent")
            if session is not None and not delegated_child:
                session.agent_id = DEFAULT_AGENT_ID
                run.agent_id = DEFAULT_AGENT_ID
            workspace_id = (
                run.workspace_id
                or (session.workspace_id if session else None)
                or agent.workspace_id
                or DEFAULT_WORKSPACE_ID
            )
            workspace = db.get(Workspace, workspace_id) or db.get(Workspace, DEFAULT_WORKSPACE_ID)
            if workspace is None:
                raise ModelConfigurationError("当前 Agent 没有可用工作区")

            messages = _prepare_session_history(db, session) if session else []
            agent_tool_ids = list(getattr(agent, "tool_ids", []) or [])
            agent_skill_ids = list(getattr(agent, "skill_ids", []) or [])
            session_skill_ids = list(getattr(session, "skill_ids", []) or []) if session else []
            configured_skill_ids = session_skill_ids or agent_skill_ids
            permission_mode = str(getattr(session, "permission_mode", "smart") or "smart")
            allowed_tool_names = _allowed_runtime_tool_names(agent_tool_ids)
            skill_instructions = _read_selected_skill_instructions(db, configured_skill_ids)
            todo_state = _session_todo_state(db, session.id if session else None)
            effective_system_prompt = agent.system_prompt
            memory_filters = [Memory.scope == "global"]
            if workspace:
                memory_filters.append((Memory.scope == "workspace") & (Memory.scope_id == workspace.id))
            if session:
                memory_filters.append((Memory.scope == "session") & (Memory.scope_id == session.id))
            memories = list(db.scalars(select(Memory).where(or_(*memory_filters)).order_by(Memory.pinned.desc(), Memory.updated_at.desc())))

            if frozen_binding:
                frozen_agent_id = str(frozen_binding.get("agent_id") or "").strip()
                if frozen_agent_id and agent.id != frozen_agent_id:
                    raise RuntimeError("冻结的子 Agent 已不存在或已被替换，已拒绝续跑")
                required = (
                    "workspace_root",
                    "model_connection_id",
                    "provider",
                    "base_url",
                    "secret_ref",
                    "model_id",
                    "custom_headers_digest",
                )
                missing = [key for key in required if not str(frozen_binding.get(key) or "").strip()]
                if missing:
                    raise RuntimeError(f"运行快照缺少冻结配置：{', '.join(missing)}")
                connection = db.get(ModelConnection, str(frozen_binding["model_connection_id"]))
                if connection is None or not connection.enabled:
                    raise RuntimeError("冻结的模型连接已不存在或被禁用，已拒绝续跑")
                connection_changed = (
                    connection.provider != str(frozen_binding["provider"])
                    or connection.base_url != str(frozen_binding["base_url"])
                    or connection.secret_ref != str(frozen_binding["secret_ref"])
                    or _configuration_digest(connection.custom_headers or {})
                    != str(frozen_binding["custom_headers_digest"])
                )
                if connection_changed:
                    raise RuntimeError("模型连接配置在审批等待期间已改变，已拒绝续跑")
                workspace_root = str(frozen_binding["workspace_root"])
                provider_config = ProviderConfig(
                    provider=str(frozen_binding["provider"]),
                    base_url=str(frozen_binding["base_url"]),
                    secret_ref=str(frozen_binding["secret_ref"]),
                    model_id=str(frozen_binding["model_id"]),
                    model_connection_id=str(frozen_binding["model_connection_id"]),
                    thinking_level=str(frozen_binding.get("thinking_level") or "auto"),
                    custom_headers=dict(connection.custom_headers or {}),
                )
                if isinstance(frozen_binding.get("tool_ids"), list):
                    agent_tool_ids = [str(item) for item in frozen_binding["tool_ids"]]
                if isinstance(frozen_binding.get("skill_ids"), list):
                    configured_skill_ids = [str(item) for item in frozen_binding["skill_ids"]]
                if str(frozen_binding.get("permission_mode") or "").strip():
                    permission_mode = str(frozen_binding["permission_mode"])
                if isinstance(frozen_binding.get("allowed_tool_names"), list):
                    allowed_tool_names = _allowed_runtime_tool_names(
                        [str(item) for item in frozen_binding["allowed_tool_names"]]
                    )
                else:
                    # Compatibility for snapshots created before executable
                    # capability bindings were persisted.
                    allowed_tool_names = _allowed_runtime_tool_names(agent_tool_ids)
                if isinstance(frozen_binding.get("skill_instructions"), list):
                    skill_instructions = [
                        dict(item) for item in frozen_binding["skill_instructions"] if isinstance(item, dict)
                    ]
                else:
                    skill_instructions = _read_selected_skill_instructions(db, configured_skill_ids)
                if isinstance(frozen_binding.get("todo_state"), list):
                    todo_state = list(frozen_binding["todo_state"])
                if "agent_system_prompt" in frozen_binding:
                    effective_system_prompt = str(frozen_binding["agent_system_prompt"] or "")
                # A persisted child snapshot is never allowed to regain task
                # from a hand-edited/legacy binding during approval resume.
                if frozen_binding.get("delegation_version"):
                    allowed_tool_names = [name for name in allowed_tool_names if name != "task"]
                    if "max_run_seconds" in frozen_binding:
                        frozen_limit = frozen_binding.get("max_run_seconds")
                        runtime_max_run_seconds = (
                            max(0.0, float(frozen_limit))
                            if frozen_limit is not None
                            else None
                        )
            else:
                connection = _effective_connection(db, session, agent)
                if connection is None:
                    raise ModelConfigurationError("当前 Agent 没有可用模型连接")
                session_model = None
                if session is not None and (
                    not session.model_connection_id or session.model_connection_id == connection.id
                ):
                    session_model = session.model_id
                agent_model = agent.model_id if (
                    not agent.model_connection_id or agent.model_connection_id == connection.id
                ) else None
                model_id = session_model or agent_model or connection.default_model
                if not model_id:
                    candidates = [*(connection.discovered_models or []), *(connection.manual_models or [])]
                    model_id = candidates[0] if candidates else None
                if not model_id:
                    raise ModelConfigurationError("模型连接中没有可用模型，请发现模型或填写手动模型 ID")
                thinking_level = _explicit_setting(
                    session.thinking_level if session is not None else None,
                    agent.thinking_level,
                    connection.thinking_level,
                ) or "auto"
                workspace_root = workspace.root_path
                provider_config = ProviderConfig(
                    provider=connection.provider,
                    base_url=connection.base_url,
                    secret_ref=connection.secret_ref,
                    model_id=model_id,
                    model_connection_id=connection.id,
                    thinking_level=thinking_level,
                    custom_headers=dict(connection.custom_headers or {}),
                )
                frozen_binding = {
                    "workspace_root": workspace_root,
                    "model_connection_id": connection.id,
                    "provider": connection.provider,
                    "base_url": connection.base_url,
                    "secret_ref": connection.secret_ref,
                    "model_id": model_id,
                    "thinking_level": thinking_level,
                    "permission_mode": permission_mode,
                    "skill_ids": configured_skill_ids,
                    "tool_ids": agent_tool_ids,
                    "allowed_tool_names": allowed_tool_names,
                    "skill_instructions": skill_instructions,
                    "todo_state": todo_state,
                    # Header values may contain credentials. Persist only a
                    # digest and reject resume if the live values drift.
                    "custom_headers_digest": _configuration_digest(connection.custom_headers or {}),
                }

            delegate_catalog_prompt = "" if frozen_binding.get("delegation_version") else _delegate_catalog_prompt(db)

            context = {
                "system_prompt": effective_system_prompt,
                # Description remains UI metadata. This system-generated list
                # is capability discovery only: it gives the fixed coordinator
                # the exact enabled child IDs needed by the task tool without
                # exposing child prompts, credentials, or workspaces.
                "agent_instructions": delegate_catalog_prompt,
                "workspace_rules": f"仅访问工作区：{workspace_root}",
                "summary": session.context_summary if session else "",
                "memories": [{"content": item.content, "pinned": item.pinned} for item in memories],
                "recent_messages": messages,
                "mode": "auto",
                "workspace_root": workspace_root,
                "provider": provider_config,
                "model_connection_id": provider_config.model_connection_id,
                "runtime_binding": frozen_binding,
                # These selections are durable configuration metadata. The
                # active runtime only acts on capabilities it explicitly
                # implements; it must not infer executable permissions merely
                # from a catalog selection.
                "permission_mode": permission_mode,
                "skill_ids": configured_skill_ids,
                "tool_ids": agent_tool_ids,
                "allowed_tool_names": allowed_tool_names,
                "skill_instructions": skill_instructions,
                "todo_state": todo_state,
                "max_run_seconds": runtime_max_run_seconds,
            }
            db.commit()

        task_delegate = None
        if not context["runtime_binding"].get("delegation_version"):
            task_delegate = _SubagentTaskDelegate(
                parent_run_id=run_id,
                parent_agent_id=agent.id,
                parent_binding=dict(context["runtime_binding"]),
                parent_allowed_tool_names=list(context["allowed_tool_names"]),
                permission_mode=str(context["permission_mode"]),
            )

        runtime = AgentRuntime(
            model_call=build_model_call(context["provider"]),
            tool_registry=create_default_registry(
                context["workspace_root"],
                allowed_tool_names=context["allowed_tool_names"],
                permission_mode=context["permission_mode"],
                skill_instructions=context["skill_instructions"],
                todo_state=context["todo_state"],
                task_delegate=task_delegate,
            ),
            context_manager=ContextManager(max_tokens=settings.context_limit_tokens),
            event_sink=RunCoordinator._event_sink(run_id),
            stream_sink=RunCoordinator._stream_sink(run_id),
            config=RuntimeConfig(
                max_steps=settings.max_steps,
                max_tool_calls=settings.max_tool_calls,
                identical_call_limit=settings.max_identical_calls,
                no_progress_limit=settings.no_progress_limit,
                model_timeout_seconds=settings.model_timeout_seconds,
                max_run_seconds=context["max_run_seconds"],
            ),
            checkpointer=coordinator.checkpointer,
        )
        return runtime, context

    @staticmethod
    def _delegation_link(
        db: Any,
        run: Run,
        runtime_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Find the durable parent/task link for a delegated child run."""

        binding = dict(runtime_binding or {})
        if binding.get("delegation_version") and binding.get("team_task_id"):
            return {
                "team_task_id": str(binding["team_task_id"]),
                "parent_run_id": str(binding.get("parent_run_id") or ""),
                "parent_agent_id": str(binding.get("parent_agent_id") or ""),
                "parent_session_id": binding.get("parent_session_id"),
            }
        link = db.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.event_type == "delegation_link")
            .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
        )
        if link is None or not isinstance(link.payload, dict):
            return None
        payload = link.payload
        task_id = str(payload.get("team_task_id") or "").strip()
        if not task_id:
            return None
        return {
            "team_task_id": task_id,
            "parent_run_id": str(payload.get("parent_run_id") or ""),
            "parent_agent_id": str(payload.get("parent_agent_id") or ""),
            "parent_session_id": payload.get("parent_session_id"),
        }

    @staticmethod
    def _sync_delegated_child_state(
        db: Any,
        run: Run,
        *,
        status: str,
        output: str | None = None,
        stop_reason: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
        pending_approval: dict[str, Any] | None = None,
        steps: int = 0,
        tool_calls: int = 0,
        usage: dict[str, Any] | None = None,
        workspace_changed: bool = False,
        runtime_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically mirror a child state into its task and parent audit trail.

        ``Run`` is updated by the caller in the same database transaction.
        This method intentionally keeps a waiting child as ``in_progress`` on
        the task board, creates a distinct waiting message, and emits one
        durable parent event.  Terminal states clear the lease and produce an
        idempotent result message instead of overwriting the waiting record.
        """

        link = RunCoordinator._delegation_link(db, run, runtime_binding)
        if link is None:
            return None
        task = db.get(TeamTask, link["team_task_id"])
        if task is None:
            return None
        existing_child_run_id = str((task.result or {}).get("child_run_id") or "")
        if existing_child_run_id and existing_child_run_id != run.id:
            return None

        binding = dict(runtime_binding or {})
        if not binding:
            snapshot = db.scalar(
                select(RunEvent)
                .where(RunEvent.run_id == run.id, RunEvent.event_type == "runtime_snapshot")
                .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
            )
            if snapshot is not None and isinstance(snapshot.payload, dict):
                stored_binding = snapshot.payload.get("runtime_binding")
                if isinstance(stored_binding, dict):
                    binding = dict(stored_binding)
        if not binding:
            # Failures before a first runtime snapshot still have the public
            # frozen selection captured when the TeamTask was created.
            stored_binding = (task.result or {}).get("binding")
            if isinstance(stored_binding, dict):
                binding = dict(stored_binding)
        child = db.get(Agent, run.agent_id or task.assignee_agent_id)
        child_id = child.id if child is not None else (run.agent_id or task.assignee_agent_id or "")
        child_name = _single_line(child.name, limit=120) if child is not None else "Child Agent"
        public_binding = _SubagentTaskDelegate._public_binding(binding) if binding else {}
        safe_pending = _SubagentTaskDelegate._safe_pending_summary(pending_approval)
        payload = {
            "task_id": task.id,
            "child_run_id": run.id,
            "parent_run_id": link["parent_run_id"],
            "parent_session_id": run.session_id or link.get("parent_session_id"),
            "agent": {"id": child_id, "name": child_name},
            "status": status,
            "output": str(output or "")[:_DELEGATE_OUTPUT_LIMIT],
            "output_truncated": len(str(output or "")) > _DELEGATE_OUTPUT_LIMIT,
            "stop_reason": _single_line(stop_reason, limit=200),
            "error_code": _single_line(error_code, limit=120),
            "error": _single_line(error, limit=2_000),
            "pending_approval": safe_pending,
            "steps": max(0, int(steps or 0)),
            "tool_calls": max(0, int(tool_calls or 0)),
            "workspace_changed": bool(workspace_changed),
            "usage": normalize_usage(usage),
            "binding": public_binding,
        }
        task_status = "in_progress" if status == "awaiting_approval" else (
            "completed" if status == "completed" else "blocked"
        )
        changed = (
            task.status != task_status
            or _canonical_arguments(task.result) != _canonical_arguments(payload)
        )
        if changed:
            task.status = task_status
            task.result = payload
            if task_status != "in_progress":
                task.lease_owner = None
                task.lease_expires_at = None
            task.version += 1

        if status == "awaiting_approval":
            pending_id = str((pending_approval or {}).get("id") or "pending")
            message_key = "delegate-awaiting-approval:%s:%s" % (
                task.id,
                hashlib.sha256(pending_id.encode("utf-8")).hexdigest()[:24],
            )
            message_type = "TASK_AWAITING_APPROVAL"
        else:
            message_key = f"delegate-result:{task.id}"
            message_type = "TASK_COMPLETED" if status == "completed" else "BLOCKED"
        message = db.scalar(select(AgentMessage).where(AgentMessage.idempotency_key == message_key))
        if message is None:
            db.add(AgentMessage(
                team_id=task.team_id,
                task_id=task.id,
                sender_agent_id=child_id or None,
                recipient_agent_id=link["parent_agent_id"] or None,
                message_type=message_type,
                payload=payload,
                idempotency_key=message_key,
                hop_count=1,
            ))
        elif _canonical_arguments(message.payload) != _canonical_arguments(payload) or message.message_type != message_type:
            message.message_type = message_type
            message.payload = payload

        event_type = {
            "awaiting_approval": "delegated_child_awaiting_approval",
            "completed": "delegated_child_completed",
            "stopped": "delegated_child_stopped",
            "failed": "delegated_child_failed",
        }.get(status, "delegated_child_incomplete")
        continuation_queued = False
        if status in {"completed", "failed", "stopped"} and link["parent_run_id"]:
            parent = db.get(Run, link["parent_run_id"])
            # The parent was deliberately stopped rather than completed while
            # the child waited.  Only that exact state can be safely resumed;
            # a still-active parent will naturally receive the synchronous
            # delegate result, and unrelated stopped runs must remain stopped.
            if (
                parent is not None
                and parent.status == "stopped"
                and parent.stop_reason == "delegated_child_awaiting_approval"
            ):
                parent.status = "received"
                parent.stop_reason = None
                parent.error_message = None
                parent.finished_at = None
                continuation_queued = True
                db.add(RunEvent(
                    run_id=parent.id,
                    event_type="delegated_child_continuation_queued",
                    payload={
                        "child_run_id": run.id,
                        "task_id": task.id,
                        "child_status": status,
                    },
                ))

        parent_event = {
            "type": event_type,
            "parent_run_id": link["parent_run_id"],
            "task_id": task.id,
            "child_run_id": run.id,
            "child_agent_id": child_id,
            "status": status,
            "pending_approval": safe_pending,
            "stop_reason": _single_line(stop_reason, limit=200),
            "error_code": _single_line(error_code, limit=120),
            "continuation_queued": continuation_queued,
        }
        if changed and link["parent_run_id"]:
            db.add(RunEvent(
                run_id=link["parent_run_id"],
                event_type=event_type,
                payload=parent_event,
            ))

        # The child result is visible in the parent conversation, but it is
        # explicitly labelled as a child result.  The parent run itself is
        # stopped while approval is outstanding and is never presented as the
        # one that summarized this result.
        if changed and status in {"completed", "failed", "stopped"} and run.session_id:
            if status == "completed":
                message_content = str(output or "").strip() or "Child Agent completed without text output."
            else:
                reason = _single_line(error or stop_reason, limit=1_000) or "no further detail"
                message_content = f"Child Agent {child_name} did not complete ({status}): {reason}"
            db.add(ChatMessage(
                session_id=run.session_id,
                role="assistant",
                content=message_content,
                extra={
                    "run_id": run.id,
                    "delegated_child": True,
                    "child_agent_id": child_id,
                    "child_agent_name": child_name,
                    "delegated_status": status,
                    "parent_run_id": link["parent_run_id"],
                    "team_task_id": task.id,
                },
            ))
        return parent_event if (changed or continuation_queued) and link["parent_run_id"] else None

    def reconcile_delegated_child_terminal(
        self,
        db: Any,
        run: Run,
        *,
        status: str,
        stop_reason: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any] | None:
        """Expose a transaction-local terminal sync for approval rejection."""

        return self._sync_delegated_child_state(
            db,
            run,
            status=status,
            stop_reason=stop_reason,
            error=error,
            error_code=error_code,
        )

    @staticmethod
    def _persist_outcome(run_id: str, outcome: RunOutcome) -> None:
        snapshot = RunCoordinator._runtime_snapshot(outcome)
        publish_type = {
            "awaiting_approval": "approval_requested",
            "completed": "run_completed",
            "stopped": "run_stopped",
            "failed": "model_failed",
        }.get(outcome.status)
        publish_event = next(
            (
                _json_safe(event)
                for event in reversed(outcome.events)
                if event.get("type") == publish_type
            ),
            None,
        )
        persisted = False
        parent_bridge_event: dict[str, Any] | None = None
        with database_module.SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None:
                return
            run.status = outcome.status
            run.current_step = outcome.steps
            run.tool_calls = outcome.tool_calls
            run.stop_reason = outcome.stop_reason
            run.error_message = outcome.error
            if outcome.status in {"completed", "failed", "stopped"}:
                run.finished_at = _utcnow()
            db.add(RunEvent(run_id=run_id, event_type="runtime_snapshot", payload=snapshot))

            parent_bridge_event = RunCoordinator._sync_delegated_child_state(
                db,
                run,
                status=outcome.status,
                output=outcome.output,
                stop_reason=outcome.stop_reason,
                error=outcome.error,
                pending_approval=outcome.pending_approval,
                steps=outcome.steps,
                tool_calls=outcome.tool_calls,
                usage=normalize_usage(outcome.usage),
                workspace_changed=_SubagentTaskDelegate._changed_workspace(outcome),
                runtime_binding=outcome.runtime_binding,
            )

            usage = normalize_usage(outcome.usage)
            if usage["request_count"]:
                connection_id = usage["model_connection_id"]
                record = db.scalar(select(UsageRecord).where(UsageRecord.run_id == run_id))
                if record is None:
                    record = UsageRecord(
                        run_id=run_id,
                        session_id=run.session_id,
                        agent_id=run.agent_id,
                        model_connection_id=connection_id,
                        model_id=usage["model_id"],
                        provider=usage["provider"],
                    )
                    db.add(record)
                record.model_connection_id = connection_id
                record.model_id = usage["model_id"]
                record.provider = usage["provider"]
                record.request_count = usage["request_count"]
                record.input_tokens = usage["input_tokens"]
                record.output_tokens = usage["output_tokens"]
                record.cache_creation_tokens = usage["cache_creation_tokens"]
                record.cache_read_tokens = usage["cache_read_tokens"]
                record.total_tokens = usage["total_tokens"]
                record.cost_usd = usage["cost_usd"]

            if outcome.status == "awaiting_approval" and outcome.pending_approval:
                pending = outcome.pending_approval
                existing_pending = list(db.scalars(
                    select(Approval)
                    .where(Approval.run_id == run_id, Approval.status == "pending")
                    .order_by(Approval.created_at.asc(), Approval.id.asc())
                ))
                matching: Approval | None = None
                for existing in existing_pending:
                    if matching is None and approval_matches_pending(existing, pending):
                        matching = existing
                        continue
                    existing.status = "superseded"
                    existing.decided_at = _utcnow()
                    db.add(RunEvent(
                        run_id=run_id,
                        event_type="approval_superseded",
                        payload={"approval_id": existing.id, "reason": "pending_call_changed"},
                    ))
                if matching is None:
                    db.add(Approval(
                        run_id=run_id,
                        tool_name=str(pending.get("tool_name") or "unknown"),
                        arguments=dict(pending.get("arguments") or {}),
                        reason=str(pending.get("reason") or "该工具会修改本机状态，需要你的确认"),
                    ))
            elif (
                outcome.status == "completed"
                and outcome.output
                and run.session_id
                and not outcome.runtime_binding.get("delegation_version")
            ):
                db.add(ChatMessage(session_id=run.session_id, role="assistant", content=outcome.output, extra={"run_id": run_id}))
                db.flush()

            if run.session_id:
                session = db.get(Session, run.session_id)
                if session is not None:
                    session.updated_at = _utcnow()
                    _prepare_session_history(db, session)
            db.commit()
            persisted = True
        if persisted and publish_event is not None:
            run_stream_broker.publish(run_id, publish_event)
        if persisted and parent_bridge_event is not None:
            parent_run_id = str(parent_bridge_event.get("parent_run_id") or "")
            if parent_run_id:
                run_stream_broker.publish(parent_run_id, parent_bridge_event)
        if persisted:
            coordinator.launch_parent_continuation_if_queued(parent_bridge_event)

    async def _execute(self, run_id: str) -> None:
        try:
            runtime, context = self._resolve_runtime(run_id)
            outcome = await runtime.run(
                system_prompt=context["system_prompt"],
                agent_instructions=context["agent_instructions"],
                workspace_rules=context["workspace_rules"],
                summary=context["summary"],
                memories=context["memories"],
                recent_messages=context["recent_messages"],
                mode=context["mode"],
                thread_id=run_id,
            )
            outcome.runtime_binding = {
                **dict(context["runtime_binding"]),
                **runtime.tool_registry.runtime_state(),
            }
            self._persist_outcome(run_id, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._persist_failure(run_id, exc)

    async def _resume(self, run_id: str) -> None:
        try:
            with database_module.SessionLocal() as db:
                snapshot = db.scalar(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.event_type == "runtime_snapshot")
                    .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
                )
                if snapshot is None:
                    raise RuntimeError("缺少可恢复的运行快照")
                prior = self._outcome_from_snapshot(snapshot.payload)
            if not prior.runtime_binding:
                raise RuntimeError("运行快照缺少冻结配置，已拒绝在可变环境中续跑")
            runtime, context = self._resolve_runtime(run_id, runtime_binding=prior.runtime_binding)
            outcome = await runtime.resume_after_approval(prior, thread_id=run_id)
            outcome.runtime_binding = {
                **dict(context["runtime_binding"]),
                **runtime.tool_registry.runtime_state(),
            }
            self._persist_outcome(run_id, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._persist_failure(run_id, exc)

    async def _resume_delegated_child(self, run_id: str) -> None:
        """Let the original coordinator model consume a terminal child result."""

        try:
            with database_module.SessionLocal() as db:
                snapshot = db.scalar(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.event_type == "runtime_snapshot")
                    .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
                )
                if snapshot is None:
                    raise RuntimeError("delegated child continuation is missing the parent snapshot")
                prior = self._outcome_from_snapshot(snapshot.payload)
            if not prior.runtime_binding:
                raise RuntimeError("delegated child continuation is missing the frozen parent binding")
            runtime, context = self._resolve_runtime(run_id, runtime_binding=prior.runtime_binding)
            outcome = await runtime.resume_after_delegated_child(prior, thread_id=run_id)
            outcome.runtime_binding = {
                **dict(context["runtime_binding"]),
                **runtime.tool_registry.runtime_state(),
            }
            self._persist_outcome(run_id, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._persist_failure(run_id, exc)

    @staticmethod
    def _persist_failure(run_id: str, error: BaseException) -> None:
        parent_bridge_event: dict[str, Any] | None = None
        with database_module.SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None:
                return
            run.status = "failed"
            run.error_code = type(error).__name__
            run.error_message = str(error) or type(error).__name__
            run.finished_at = _utcnow()
            db.add(RunEvent(run_id=run_id, event_type="integration_failed", payload={"error": run.error_message}))
            parent_bridge_event = RunCoordinator._sync_delegated_child_state(
                db,
                run,
                status="failed",
                error=run.error_message,
                error_code="delegate_execution_error",
            )
            db.commit()
            error_message = run.error_message
        run_stream_broker.publish(run_id, {"type": "integration_failed", "error": error_message})
        if parent_bridge_event is not None and parent_bridge_event.get("parent_run_id"):
            run_stream_broker.publish(str(parent_bridge_event["parent_run_id"]), parent_bridge_event)
        coordinator.launch_parent_continuation_if_queued(parent_bridge_event)


coordinator = RunCoordinator()
