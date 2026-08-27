"""Durable semantic task plans and recovery packets for root conversation runs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select, text

from app import database as database_module
from app.database import ChatMessage, DurableTask, PlanStep, Run
from app.services.task_graph import dependency_map, replace_dependencies, validate_dependency_graph


RESUMABLE_TASK_STATUSES = frozenset({"running", "waiting", "paused", "needs_recovery", "blocked"})
_CONTINUATION_PHRASES = frozenset({
    "继续", "继续工作", "继续任务", "继续刚刚的工作", "继续之前的工作", "接着做", "恢复任务",
    "continue", "continue the task", "continue the previous task", "resume", "resume task",
})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_continuation_request(content: str) -> bool:
    normalized = " ".join(content.strip().casefold().split()).rstrip("。.!！?？")
    if normalized in _CONTINUATION_PHRASES:
        return True
    return len(normalized) <= 80 and (
        normalized.startswith("继续刚才")
        or normalized.startswith("继续刚刚")
        or normalized.startswith("继续之前")
        or normalized.startswith("继续未完成")
        or normalized.startswith("接着刚才")
        or normalized.startswith("接着之前")
        or normalized.startswith("resume the previous")
        or normalized.startswith("continue where")
    )


def steps_for_task(db: Any, task_id: str) -> list[PlanStep]:
    return list(db.scalars(
        select(PlanStep)
        .where(PlanStep.task_id == task_id)
        .order_by(PlanStep.position.asc(), PlanStep.created_at.asc(), PlanStep.id.asc())
    ))


def latest_resumable_task(db: Any, session_id: str) -> DurableTask | None:
    return db.scalar(
        select(DurableTask)
        .where(DurableTask.session_id == session_id, DurableTask.status.in_(RESUMABLE_TASK_STATUSES))
        .order_by(DurableTask.updated_at.desc(), DurableTask.created_at.desc(), DurableTask.id.desc())
        .limit(1)
    )


def _goal_for_run(db: Any, run: Run) -> str:
    if run.turn_id:
        message = db.scalar(select(ChatMessage).where(
            ChatMessage.turn_id == run.turn_id,
            ChatMessage.role == "user",
        ))
        if message is not None and str(message.content or "").strip():
            return str(message.content).strip()
    return "Continue the accepted user task."


def bind_recovery_task(db: Any, run: Run, content: str) -> DurableTask | None:
    """Attach a continuation turn to the latest unfinished task in its session."""

    if not run.session_id or not is_continuation_request(content):
        return None
    task = latest_resumable_task(db, run.session_id)
    if task is None:
        return None
    previous = db.scalar(
        select(Run)
        .where(Run.task_id == task.id, Run.id != run.id)
        .order_by(Run.started_at.desc(), Run.id.desc())
        .limit(1)
    )
    run.task_id = task.id
    run.plan_step_id = task.active_step_id
    run.run_kind = "recovery"
    run.resumed_from_run_id = previous.id if previous is not None else None
    task.status = "running"
    task.resume_summary = "用户请求继续未完成任务；新运行将先核验上次活动步骤。"
    return task


def _normalize_todos(todos: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(todos, start=1):
        content = str(raw.get("content") or "").strip()
        if not content:
            continue
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in {"pending", "in_progress", "completed", "cancelled"}:
            status = "pending"
        external_id = str(raw.get("id") or "").strip()
        if not external_id:
            raise ValueError(f"todo {index} requires a stable id")
        raw_dependencies = raw.get("depends_on", raw.get("blockedBy", []))
        if raw_dependencies is None:
            raw_dependencies = []
        if not isinstance(raw_dependencies, (list, tuple)):
            raise ValueError(f"todo {external_id} depends_on must be an array")
        dependencies = list(dict.fromkeys(
            str(value).strip() for value in raw_dependencies if str(value).strip()
        ))
        executor_kind = str(raw.get("executor_kind") or raw.get("executor") or "main").strip().lower()
        if executor_kind not in {"main", "subagent", "background"}:
            raise ValueError(f"todo {external_id} has invalid executor_kind")
        assigned_agent_id = str(raw.get("agent_id") or raw.get("assigned_agent_id") or "").strip()
        if executor_kind == "subagent" and not assigned_agent_id:
            raise ValueError(f"todo {external_id} requires agent_id for subagent execution")
        workspace_mode = str(raw.get("workspace_mode") or "shared").strip().lower()
        if workspace_mode not in {"shared", "worktree"}:
            raise ValueError(f"todo {external_id} has invalid workspace_mode")
        normalized.append({
            "id": external_id,
            "content": content,
            "status": status,
            "active_form": str(raw.get("active_form") or raw.get("activeForm") or "").strip(),
            "depends_on": dependencies,
            "executor_kind": executor_kind,
            "agent_id": assigned_agent_id,
            "workspace_mode": workspace_mode,
        })
    validate_dependency_graph(normalized)
    return normalized


def sync_todos_for_run(run_id: str, todos: Iterable[Mapping[str, Any]]) -> None:
    """Persist one successful TodoWrite as the run's semantic task plan."""

    normalized = _normalize_todos(todos)
    with database_module.SessionLocal() as db:
        # TodoWrite executes in a worker thread while stop/outcome persistence
        # may run in another transaction. Claim the SQLite writer slot before
        # reading the Run so commit order also determines lifecycle order.
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
            run = db.get(Run, run_id)
        else:
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or not run.session_id:
            return
        if run.status in {"completed", "failed", "stopped"}:
            return
        task = db.get(DurableTask, run.task_id) if run.task_id else None
        if task is None and not normalized:
            return
        if task is None:
            task = DurableTask(
                session_id=run.session_id,
                origin_turn_id=run.turn_id,
                goal=_goal_for_run(db, run),
                status="planning",
            )
            db.add(task)
            db.flush()
            run.task_id = task.id

        existing = {step.external_id: step for step in steps_for_task(db, task.id)}
        cleared_incomplete_plan = not normalized and any(
            step.status not in {"completed", "cancelled"} for step in existing.values()
        )
        retained: set[str] = set()
        now = utcnow()
        for position, item in enumerate(normalized, start=1):
            external_id = item["id"]
            retained.add(external_id)
            step = existing.get(external_id)
            if step is None:
                step = PlanStep(task_id=task.id, external_id=external_id, position=position, title=item["content"])
                db.add(step)
            previous_status = step.status
            step.position = position
            step.title = item["content"]
            step.description = item["active_form"]
            requested_status = item["status"]
            step.status = (
                "completed"
                if previous_status == "completed" and requested_status != "completed"
                else requested_status
            )
            step.executor_kind = item["executor_kind"]
            step.assigned_agent_id = item["agent_id"] or None
            step.workspace_mode = item["workspace_mode"]
            step.last_run_id = run.id
            if step.status == "in_progress":
                step.started_at = step.started_at or now
                step.next_action = item["active_form"] or item["content"]
                step.remaining_work = [item["content"]]
            elif step.status == "completed":
                step.started_at = step.started_at or now
                step.completed_at = step.completed_at or now
                step.completed_work = [item["content"]]
                step.remaining_work = []
                step.next_action = ""
                if not step.result:
                    step.result = item["content"]
            elif step.status == "pending":
                step.remaining_work = [item["content"]]
                step.next_action = ""
                if previous_status == "needs_recovery":
                    step.result = "恢复核验后确认该步骤尚未开始。"
            else:
                step.next_action = ""

        for external_id, step in existing.items():
            if external_id not in retained and step.status != "completed":
                step.status = "cancelled"
                step.next_action = ""

        db.flush()
        replace_dependencies(
            db,
            task_id=task.id,
            items=normalized,
            steps_by_external_id={step.external_id: step for step in steps_for_task(db, task.id)},
        )
        db.flush()
        steps = steps_for_task(db, task.id)
        if not normalized:
            task.active_step_id = None
            run.plan_step_id = None
            task.status = "cancelled" if cleared_incomplete_plan else "completed"
            task.completed_at = now
            task.resume_summary = (
                "计划已清空；原有未完成步骤已取消。"
                if cleared_incomplete_plan else "计划中的步骤均已完成。"
            )
            db.commit()
            return
        active = next((step for step in steps if step.status == "in_progress"), None)
        active = active or next((step for step in steps if step.status == "needs_recovery"), None)
        active = active or next((step for step in steps if step.status == "pending"), None)
        task.active_step_id = active.id if active is not None else None
        run.plan_step_id = task.active_step_id
        if steps and all(step.status in {"completed", "cancelled"} for step in steps):
            task.status = "completed"
            task.completed_at = now
            task.resume_summary = "计划中的步骤均已完成。"
        else:
            task.status = "running"
            task.completed_at = None
            task.resume_summary = (
                f"当前步骤：{active.title}；下一动作：{active.next_action or active.title}"
                if active is not None else "计划已创建，等待下一步。"
            )
        db.commit()


def todo_state_for_run(db: Any, run: Run) -> list[dict[str, Any]]:
    if not run.task_id:
        return []
    dependencies = dependency_map(db, run.task_id)
    return [
        {
            "id": step.external_id,
            "content": step.title,
            "status": "in_progress" if step.status in {"needs_recovery", "failed"} else step.status,
            **({"active_form": step.description} if step.description else {}),
            **({"depends_on": dependencies.get(step.external_id, [])} if dependencies.get(step.external_id) else {}),
            **({"executor_kind": step.executor_kind} if step.executor_kind != "main" else {}),
            **({"agent_id": step.assigned_agent_id} if step.assigned_agent_id else {}),
            **({"workspace_mode": step.workspace_mode} if step.workspace_mode != "shared" else {}),
        }
        for step in steps_for_task(db, run.task_id)
        if step.status in {"pending", "in_progress", "completed", "cancelled", "needs_recovery", "failed"}
    ]


def task_checkpoint_for_run(run_id: str) -> dict[str, Any]:
    """Read the latest durable task facts for compaction from a fresh transaction."""

    with database_module.SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None or not run.task_id:
            return {}
        task = db.get(DurableTask, run.task_id)
        if task is None:
            return {}
        return json.loads(json.dumps(task_payload(db, task), ensure_ascii=False, default=str))


def recovery_prompt(db: Any, run: Run) -> str:
    if not run.task_id:
        return ""
    task = db.get(DurableTask, run.task_id)
    if task is None:
        return ""
    steps = steps_for_task(db, task.id)
    dependencies = dependency_map(db, task.id)
    packet = {
        "task_id": task.id,
        "goal": task.goal,
        "constraints": list(task.constraints or []),
        "task_status": task.status,
        "resume_summary": task.resume_summary,
        "steps": [
            {
                "id": step.external_id,
                "title": step.title,
                "status": step.status,
                "completed_work": list(step.completed_work or []),
                "remaining_work": list(step.remaining_work or []),
                "next_action": step.next_action,
                "result": step.result,
                "evidence": list(step.evidence or []),
                "depends_on": dependencies.get(step.external_id, []),
                "executor_kind": step.executor_kind,
                "assigned_agent_id": step.assigned_agent_id,
                "assigned_run_id": step.assigned_run_id,
                "claim_owner": step.claim_owner,
                "workspace_mode": step.workspace_mode,
                "attempt": step.attempt,
                "error": step.error,
            }
            for step in steps
        ],
    }
    return (
        "\n\n<durable-task-recovery>\n"
        "This backend-generated packet is the durable task source of truth. "
        "Continue the goal, do not restart completed steps, and inspect workspace facts before retrying any "
        "step marked needs_recovery. Keep the plan current with todowrite after each semantic step.\n"
        f"{json.dumps(packet, ensure_ascii=False, separators=(',', ':'))}\n"
        "</durable-task-recovery>"
    )


def transition_run_task(db: Any, run: Run, *, status: str, stop_reason: str | None = None) -> None:
    """Project a run terminal/interruption state onto its durable task."""

    if not run.task_id:
        return
    task = db.get(DurableTask, run.task_id)
    if task is None:
        return
    steps = steps_for_task(db, task.id)
    active = db.get(PlanStep, task.active_step_id) if task.active_step_id else None
    now = utcnow()
    if status == "completed" and steps and all(step.status in {"completed", "cancelled"} for step in steps):
        task.status = "completed"
        task.completed_at = now
        task.resume_summary = "计划中的步骤均已完成。"
        return
    if status == "awaiting_approval":
        return
    if status == "stopped" and stop_reason == "waiting_background":
        task.status = "waiting"
        task.resume_summary = "Waiting for a background task terminal event; the coordinator will resume automatically."
        return
    if status == "stopped" and stop_reason in {
        "delegated_child_awaiting_approval",
        "delegated_child_waiting_event",
    }:
        task.status = "waiting"
        task.resume_summary = "Waiting for a delegated child event before the coordinator continues."
        return
    if status == "stopped" and stop_reason == "user_interrupted":
        task.status = "paused"
        task.resume_summary = "用户暂停了任务；恢复时先核验活动步骤是否产生了部分结果。"
    elif status in {"failed", "stopped"}:
        task.status = "needs_recovery"
        task.resume_summary = f"运行中断（{stop_reason or status}）；活动步骤结果尚待核验。"
    elif status == "completed":
        task.status = "paused"
        task.resume_summary = "本轮运行已经结束，但持久化计划仍有未完成步骤。"
    if active is not None and active.status == "in_progress" and status in {"failed", "stopped"}:
        active.status = "needs_recovery"
        active.next_action = f"核验“{active.title}”已经产生的实际结果，再决定补做或完成。"
        active.last_run_id = run.id


def task_payload(db: Any, task: DurableTask) -> dict[str, Any]:
    dependencies = dependency_map(db, task.id)
    return {
        "id": task.id,
        "session_id": task.session_id,
        "origin_turn_id": task.origin_turn_id,
        "goal": task.goal,
        "constraints": list(task.constraints or []),
        "status": task.status,
        "active_step_id": task.active_step_id,
        "resume_summary": task.resume_summary,
        "completed_at": task.completed_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "steps": [
            {
                "id": step.id,
                "external_id": step.external_id,
                "position": step.position,
                "title": step.title,
                "description": step.description,
                "status": step.status,
                "completed_work": list(step.completed_work or []),
                "remaining_work": list(step.remaining_work or []),
                "next_action": step.next_action,
                "result": step.result,
                "evidence": list(step.evidence or []),
                "depends_on": dependencies.get(step.external_id, []),
                "executor_kind": step.executor_kind,
                "assigned_agent_id": step.assigned_agent_id,
                "assigned_run_id": step.assigned_run_id,
                "claim_owner": step.claim_owner,
                "workspace_mode": step.workspace_mode,
                "worktree_path": step.worktree_path,
                "attempt": step.attempt,
                "error": step.error,
                "last_run_id": step.last_run_id,
                "started_at": step.started_at,
                "completed_at": step.completed_at,
                "created_at": step.created_at,
                "updated_at": step.updated_at,
            }
            for step in steps_for_task(db, task.id)
        ],
    }
