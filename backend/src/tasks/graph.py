"""Dependency-aware scheduling for durable task plan steps."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import delete, select, text

from src.persistence import database as database_module
from src.persistence.database import ChatMessage, DurableTask, PlanStep, PlanStepDependency, Run
from src.tools.types import ToolResult


TERMINAL_STEP_STATUSES = frozenset({"completed", "cancelled", "failed"})
SUCCESSFUL_DEPENDENCY_STATUSES = frozenset({"completed"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _goal_for_run(db: Any, run: Run) -> str:
    if run.turn_id:
        message = db.scalar(select(ChatMessage).where(
            ChatMessage.turn_id == run.turn_id,
            ChatMessage.role == "user",
        ))
        if message is not None and str(message.content or "").strip():
            return str(message.content).strip()
    return "Complete the durable task graph."


def validate_dependency_graph(items: Iterable[Mapping[str, Any]]) -> None:
    """Reject missing, self-referential, and cyclic prerequisite edges."""

    rows = [dict(item) for item in items]
    identifiers = [str(item.get("id") or "").strip() for item in rows]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("task step ids must be unique")
    known = set(identifiers)
    graph: dict[str, tuple[str, ...]] = {}
    for item, step_id in zip(rows, identifiers, strict=True):
        dependencies = tuple(str(value).strip() for value in item.get("depends_on") or ())
        missing = [value for value in dependencies if value not in known]
        if missing:
            raise ValueError(f"step {step_id} depends on unknown step: {missing[0]}")
        if step_id in dependencies:
            raise ValueError(f"step {step_id} cannot depend on itself")
        graph[step_id] = dependencies

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise ValueError(f"task dependency cycle includes step {step_id}")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency_id in graph[step_id]:
            visit(dependency_id)
        visiting.remove(step_id)
        visited.add(step_id)

    for identifier in identifiers:
        visit(identifier)


def replace_dependencies(
    db: Any,
    *,
    task_id: str,
    items: Iterable[Mapping[str, Any]],
    steps_by_external_id: Mapping[str, PlanStep],
) -> None:
    """Replace the dependency edges for the currently retained task steps."""

    rows = [dict(item) for item in items]
    validate_dependency_graph(rows)
    task_step_ids = list(db.scalars(select(PlanStep.id).where(PlanStep.task_id == task_id)))
    if task_step_ids:
        db.execute(delete(PlanStepDependency).where(PlanStepDependency.step_id.in_(task_step_ids)))
    for item in rows:
        step = steps_by_external_id[str(item["id"])]
        for dependency_external_id in item.get("depends_on") or ():
            dependency = steps_by_external_id[str(dependency_external_id)]
            db.add(PlanStepDependency(step_id=step.id, depends_on_step_id=dependency.id))


def upsert_delegated_graph(
    parent_run_id: str,
    specs: Iterable[Mapping[str, Any]],
    *,
    graph_call_id: str | None = None,
) -> dict[str, str]:
    """Attach one explicit delegated DAG to the parent's durable task."""

    rows = [dict(spec) for spec in specs]
    validate_dependency_graph(rows)
    with database_module.SessionLocal() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
            run = db.get(Run, parent_run_id)
        else:
            run = db.scalar(select(Run).where(Run.id == parent_run_id).with_for_update())
        if run is None or not run.session_id:
            raise ValueError("parent run does not exist or has no conversation")
        task = db.get(DurableTask, run.task_id) if run.task_id else None
        if task is None:
            user_goal = "Complete the delegated task graph."
            if run.turn_id:
                message = db.scalar(select(ChatMessage).where(
                    ChatMessage.turn_id == run.turn_id,
                    ChatMessage.role == "user",
                ))
                if message is not None and str(message.content or "").strip():
                    user_goal = str(message.content).strip()
            task = DurableTask(
                session_id=run.session_id,
                origin_turn_id=run.turn_id,
                goal=user_goal,
                status="running",
            )
            db.add(task)
            db.flush()
            run.task_id = task.id

        existing_steps = list(db.scalars(
            select(PlanStep)
            .where(PlanStep.task_id == task.id)
            .order_by(PlanStep.position.asc(), PlanStep.created_at.asc(), PlanStep.id.asc())
        ))
        by_external_id = {step.external_id: step for step in existing_steps}
        next_position = max((step.position for step in existing_steps), default=0) + 1
        selected: dict[str, PlanStep] = {}
        linked_existing_step_ids: set[str] = set()
        graph_prefix = f"delegate:{graph_call_id}:" if graph_call_id else ""
        for spec in rows:
            local_external_id = str(spec["id"])
            persisted_external_id = f"{graph_prefix}{local_external_id}"
            step = by_external_id.get(persisted_external_id)
            explicit_step = by_external_id.get(local_external_id)
            if (
                step is None
                and explicit_step is not None
                and explicit_step.executor_kind == "subagent"
                and (
                    not str(spec.get("agent_id") or "").strip()
                    or explicit_step.assigned_agent_id == str(spec.get("agent_id") or "").strip()
                )
            ):
                step = explicit_step
                linked_existing_step_ids.add(step.id)
            if step is None and bool(spec.get("link_existing")):
                candidates = [
                    candidate
                    for candidate in ready_steps(db, task.id, executor_kind="subagent")
                    if candidate.assigned_run_id is None
                    and candidate.id not in linked_existing_step_ids
                    and (
                        not str(spec.get("agent_id") or "").strip()
                        or candidate.assigned_agent_id == str(spec.get("agent_id") or "").strip()
                    )
                ]
                if len(candidates) > 1:
                    delegated_title = str(spec.get("task") or "").strip()
                    exact_matches = [
                        candidate
                        for candidate in candidates
                        if delegated_title in {
                            str(candidate.title or "").strip(),
                            str(candidate.description or "").strip(),
                        }
                    ]
                    candidates = exact_matches
                if len(candidates) == 1:
                    step = candidates[0]
                    linked_existing_step_ids.add(step.id)
            if step is None:
                step = PlanStep(
                    task_id=task.id,
                    external_id=persisted_external_id,
                    position=next_position,
                    title=str(spec.get("task") or local_external_id),
                    status="pending",
                )
                next_position += 1
                db.add(step)
                db.flush()
                by_external_id[persisted_external_id] = step
            if step.id not in linked_existing_step_ids:
                step.title = str(spec.get("task") or step.title)
            step.description = str(spec.get("task") or step.description)
            step.executor_kind = "subagent"
            step.assigned_agent_id = str(spec.get("agent_id") or "") or None
            step.workspace_mode = str(spec.get("workspace_mode") or "shared")
            step.remaining_work = [] if step.status == "completed" else [step.title]
            selected[local_external_id] = step

        selected_step_ids = [
            step.id for step in selected.values() if step.id not in linked_existing_step_ids
        ]
        if selected_step_ids:
            db.execute(delete(PlanStepDependency).where(
                PlanStepDependency.step_id.in_(selected_step_ids)
            ))
        for spec in rows:
            step = selected[str(spec["id"])]
            if step.id in linked_existing_step_ids:
                continue
            for dependency_external_id in spec.get("depends_on") or ():
                dependency = selected[str(dependency_external_id)]
                db.add(PlanStepDependency(step_id=step.id, depends_on_step_id=dependency.id))
        db.flush()
        refresh_task_state(db, task.id)
        first_ready = next(iter(ready_steps(db, task.id)), None)
        task.active_step_id = first_ready.id if first_ready is not None else task.active_step_id
        run.plan_step_id = task.active_step_id
        db.commit()
        return {external_id: step.id for external_id, step in selected.items()}


def dependency_map(db: Any, task_id: str) -> dict[str, list[str]]:
    steps = list(db.scalars(select(PlanStep).where(PlanStep.task_id == task_id)))
    external_by_id = {step.id: step.external_id for step in steps}
    result = {step.external_id: [] for step in steps}
    if not steps:
        return result
    edges = list(db.scalars(
        select(PlanStepDependency).where(PlanStepDependency.step_id.in_(external_by_id))
    ))
    for edge in edges:
        step_external_id = external_by_id.get(edge.step_id)
        dependency_external_id = external_by_id.get(edge.depends_on_step_id)
        if step_external_id and dependency_external_id:
            result[step_external_id].append(dependency_external_id)
    for dependencies in result.values():
        dependencies.sort()
    return result


def ready_steps(db: Any, task_id: str, *, executor_kind: str | None = None) -> list[PlanStep]:
    """Return pending steps whose prerequisites have all completed."""

    steps = list(db.scalars(
        select(PlanStep)
        .where(PlanStep.task_id == task_id)
        .order_by(PlanStep.position.asc(), PlanStep.created_at.asc(), PlanStep.id.asc())
    ))
    by_id = {step.id: step for step in steps}
    dependencies: dict[str, list[str]] = {step.id: [] for step in steps}
    if by_id:
        for edge in db.scalars(
            select(PlanStepDependency).where(PlanStepDependency.step_id.in_(by_id))
        ):
            dependencies[edge.step_id].append(edge.depends_on_step_id)
    return [
        step
        for step in steps
        if step.status == "pending"
        and (executor_kind is None or step.executor_kind == executor_kind)
        and all(
            by_id[dependency_id].status in SUCCESSFUL_DEPENDENCY_STATUSES
            for dependency_id in dependencies[step.id]
        )
    ]


def claim_step(
    step_id: str,
    *,
    assigned_run_id: str | None = None,
    assigned_agent_id: str | None = None,
    claim_owner: str | None = None,
) -> bool:
    """Atomically claim a ready step exactly once."""

    with database_module.SessionLocal() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
            step = db.get(PlanStep, step_id)
        else:
            step = db.scalar(select(PlanStep).where(PlanStep.id == step_id).with_for_update())
        if step is None or step.status != "pending":
            db.rollback()
            return False
        dependency_ids = list(db.scalars(
            select(PlanStepDependency.depends_on_step_id).where(PlanStepDependency.step_id == step.id)
        ))
        if dependency_ids:
            statuses = list(db.scalars(select(PlanStep.status).where(PlanStep.id.in_(dependency_ids))))
            if len(statuses) != len(dependency_ids) or any(
                status not in SUCCESSFUL_DEPENDENCY_STATUSES for status in statuses
            ):
                db.rollback()
                return False
        step.status = "in_progress"
        step.started_at = step.started_at or utcnow()
        step.assigned_run_id = assigned_run_id
        step.assigned_agent_id = assigned_agent_id or step.assigned_agent_id
        step.claim_owner = claim_owner or step.claim_owner
        step.attempt = int(step.attempt or 0) + 1
        step.error = None
        task = db.get(DurableTask, step.task_id)
        if task is not None:
            task.status = "running"
            task.active_step_id = step.id
        db.commit()
        return True


class TaskGraphToolStore:
    """Compatibility task-board tools backed by the canonical plan-step DAG."""

    def __init__(self, *, run_id: str) -> None:
        self.run_id = run_id

    def _task(self, db: Any) -> DurableTask:
        run = db.get(Run, self.run_id)
        if run is None or not run.session_id:
            raise ValueError("parent run does not exist or has no conversation")
        task = db.get(DurableTask, run.task_id) if run.task_id else None
        if task is None:
            task = DurableTask(
                session_id=run.session_id,
                origin_turn_id=run.turn_id,
                goal=_goal_for_run(db, run),
                status="running",
            )
            db.add(task)
            db.flush()
            run.task_id = task.id
        return task

    @staticmethod
    def _step(db: Any, task_id: str, task_id_value: str | int) -> PlanStep | None:
        normalized = str(task_id_value)
        step = db.get(PlanStep, normalized)
        if step is not None and step.task_id == task_id:
            return step
        return db.scalar(select(PlanStep).where(
            PlanStep.task_id == task_id,
            PlanStep.external_id == normalized,
        ))

    @staticmethod
    def _payload(db: Any, step: PlanStep) -> dict[str, Any]:
        dependencies = dependency_map(db, step.task_id).get(step.external_id, [])
        messages = [
            item
            for item in list(step.evidence or [])
            if isinstance(item, dict) and item.get("type") == "message"
        ]
        packet = next((
            dict(item.get("packet") or {})
            for item in list(step.evidence or [])
            if isinstance(item, dict) and item.get("type") == "task_packet"
        ), {})
        return {
            "id": step.external_id,
            "plan_step_id": step.id,
            "subject": step.title,
            "description": step.description,
            "status": "stopped" if step.status == "cancelled" else step.status,
            "owner": step.claim_owner,
            "blockedBy": dependencies,
            "messages": messages,
            "output": step.result,
            "packet": packet,
            "executor_kind": step.executor_kind,
            "assigned_agent_id": step.assigned_agent_id,
            "created_at": step.created_at.isoformat() if step.created_at else None,
            "updated_at": step.updated_at.isoformat() if step.updated_at else None,
        }

    def create(
        self,
        *,
        subject: str | None = None,
        description: str = "",
        prompt: str | None = None,
        packet: Mapping[str, Any] | None = None,
        tool_name: str = "task_create",
    ) -> ToolResult:
        title = str(subject or description or prompt or (packet or {}).get("objective") or "").strip()
        if not title:
            return ToolResult(tool_name, False, "task title cannot be empty", error_code="invalid_arguments")
        with database_module.SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            task = self._task(db)
            existing = list(db.scalars(select(PlanStep).where(PlanStep.task_id == task.id)))
            numeric_ids = [int(step.external_id) for step in existing if step.external_id.isdigit()]
            position = max((step.position for step in existing), default=0) + 1
            evidence = ([{"type": "task_packet", "packet": dict(packet)}] if packet else [])
            step = PlanStep(
                task_id=task.id,
                external_id=str(max(numeric_ids, default=0) + 1),
                position=position,
                title=title[:500],
                description=str(description or prompt or "")[:10_000],
                status="pending",
                remaining_work=[title[:500]],
                evidence=evidence,
            )
            db.add(step)
            db.flush()
            refresh_task_state(db, task.id)
            payload = self._payload(db, step)
            db.commit()
            return ToolResult(tool_name, True, json.dumps(payload, ensure_ascii=False), changed=True)

    def get(self, *, task_id: str | int, tool_name: str = "task_get") -> ToolResult:
        with database_module.SessionLocal() as db:
            task = self._task(db)
            step = self._step(db, task.id, task_id)
            if step is None:
                return ToolResult(tool_name, False, "task does not exist", error_code="task_not_found")
            return ToolResult(tool_name, True, json.dumps(self._payload(db, step), ensure_ascii=False))

    def list(self, *, tool_name: str = "task_list") -> ToolResult:
        with database_module.SessionLocal() as db:
            task = self._task(db)
            steps = list(db.scalars(
                select(PlanStep)
                .where(PlanStep.task_id == task.id)
                .order_by(PlanStep.position.asc(), PlanStep.created_at.asc(), PlanStep.id.asc())
            ))
            return ToolResult(
                tool_name,
                True,
                json.dumps([self._payload(db, step) for step in steps], ensure_ascii=False),
            )

    def update(
        self,
        *,
        task_id: str | int,
        status: str | None = None,
        message: str | None = None,
        add_blocked_by: Iterable[str | int] | None = None,
        remove_blocked_by: Iterable[str | int] | None = None,
        output: str | None = None,
        tool_name: str = "task_update",
    ) -> ToolResult:
        status_map = {
            "pending": "pending",
            "in_progress": "in_progress",
            "completed": "completed",
            "failed": "failed",
            "stopped": "cancelled",
            "deleted": "cancelled",
        }
        if status is not None and status not in status_map:
            return ToolResult(tool_name, False, "task status is invalid", error_code="invalid_arguments")
        with database_module.SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            task = self._task(db)
            step = self._step(db, task.id, task_id)
            if step is None:
                db.rollback()
                return ToolResult(tool_name, False, "task does not exist", error_code="task_not_found")
            steps = list(db.scalars(select(PlanStep).where(PlanStep.task_id == task.id)))
            by_external_id = {item.external_id: item for item in steps}
            dependencies = dependency_map(db, task.id)
            blocked = set(dependencies.get(step.external_id, []))
            blocked.update(str(item) for item in add_blocked_by or ())
            blocked.difference_update(str(item) for item in remove_blocked_by or ())
            if any(item not in by_external_id for item in blocked):
                db.rollback()
                return ToolResult(tool_name, False, "dependency task does not exist", error_code="task_not_found")
            dependencies[step.external_id] = sorted(blocked)
            graph_rows = [
                {"id": item.external_id, "depends_on": dependencies.get(item.external_id, [])}
                for item in steps
            ]
            try:
                replace_dependencies(
                    db,
                    task_id=task.id,
                    items=graph_rows,
                    steps_by_external_id=by_external_id,
                )
            except ValueError as exc:
                db.rollback()
                return ToolResult(tool_name, False, str(exc), error_code="invalid_task_graph")

            if status in {"in_progress", "completed"} and any(
                by_external_id[item].status not in SUCCESSFUL_DEPENDENCY_STATUSES for item in blocked
            ):
                db.rollback()
                return ToolResult(tool_name, False, "task still has unresolved dependencies", error_code="task_blocked")
            if status is not None:
                step.status = status_map[status]
                if step.status == "in_progress":
                    step.started_at = step.started_at or utcnow()
                    step.attempt = int(step.attempt or 0) + 1
                if step.status in TERMINAL_STEP_STATUSES:
                    step.completed_at = utcnow()
                    step.remaining_work = [] if step.status == "completed" else list(step.remaining_work or [step.title])
                    if step.status == "completed":
                        step.completed_work = list(step.completed_work or [step.title])
                elif step.status == "pending":
                    step.completed_at = None
            if message:
                step.evidence = [
                    *list(step.evidence or []),
                    {"type": "message", "at": utcnow().isoformat(), "message": str(message)},
                ]
            if output is not None:
                step.result = str(output)
            refresh_task_state(db, task.id)
            payload = self._payload(db, step)
            db.commit()
            return ToolResult(tool_name, True, json.dumps(payload, ensure_ascii=False), changed=True)

    def claim(self, *, task_id: str | int, owner: str = "lead") -> ToolResult:
        with database_module.SessionLocal() as db:
            task = self._task(db)
            step = self._step(db, task.id, task_id)
            if step is None:
                return ToolResult("claim_task", False, "task does not exist", error_code="task_not_found")
            step_id = step.id
            existing_owner = step.claim_owner
        normalized_owner = str(owner or "lead")[:160]
        if existing_owner not in {None, "", normalized_owner}:
            return ToolResult("claim_task", False, f"task is already claimed by {existing_owner}", error_code="task_claimed")
        if not claim_step(step_id, assigned_run_id=self.run_id, claim_owner=normalized_owner):
            return ToolResult("claim_task", False, "task is not ready to claim", error_code="task_blocked")
        with database_module.SessionLocal() as db:
            step = db.get(PlanStep, step_id)
            return ToolResult(
                "claim_task", True, json.dumps(self._payload(db, step), ensure_ascii=False), changed=True
            )


def refresh_task_state(db: Any, task_id: str) -> DurableTask | None:
    task = db.get(DurableTask, task_id)
    if task is None:
        return None
    steps = list(db.scalars(
        select(PlanStep)
        .where(PlanStep.task_id == task_id)
        .order_by(PlanStep.position.asc(), PlanStep.created_at.asc(), PlanStep.id.asc())
    ))
    now = utcnow()
    if steps and all(step.status in {"completed", "cancelled"} for step in steps):
        task.status = "completed"
        task.active_step_id = None
        task.completed_at = now
        task.resume_summary = "All task graph steps reached a terminal accepted state."
        return task
    failed = next((step for step in steps if step.status == "failed"), None)
    if failed is not None:
        task.status = "blocked"
        task.active_step_id = failed.id
        task.completed_at = None
        task.resume_summary = f"Step {failed.external_id} failed: {failed.error or failed.title}"
        return task
    active = next((step for step in steps if step.status in {"in_progress", "needs_recovery"}), None)
    active = active or next(iter(ready_steps(db, task_id)), None)
    task.status = "running"
    task.active_step_id = active.id if active is not None else None
    task.completed_at = None
    task.resume_summary = (
        f"Current step: {active.title}" if active is not None else "Pending steps are waiting for prerequisites."
    )
    return task


def settle_step(
    step_id: str,
    *,
    status: str,
    result: str = "",
    error: str | None = None,
    evidence: Iterable[Any] = (),
    assigned_run_id: str | None = None,
) -> bool:
    if status not in {"completed", "failed", "cancelled"}:
        raise ValueError("step terminal status must be completed, failed, or cancelled")
    with database_module.SessionLocal() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
            step = db.get(PlanStep, step_id)
        else:
            step = db.scalar(select(PlanStep).where(PlanStep.id == step_id).with_for_update())
        if step is None or step.status not in {"in_progress", "needs_recovery", "pending"}:
            db.rollback()
            return False
        step.status = status
        step.result = str(result or "")
        step.error = str(error) if error else None
        step.evidence = list(evidence)
        step.assigned_run_id = assigned_run_id or step.assigned_run_id
        step.next_action = ""
        step.remaining_work = [] if status == "completed" else list(step.remaining_work or [step.title])
        if status == "completed":
            step.completed_work = list(step.completed_work or [step.title])
        step.completed_at = utcnow()
        refresh_task_state(db, step.task_id)
        db.commit()
        return True
