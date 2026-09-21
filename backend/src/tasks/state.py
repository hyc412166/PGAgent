"""Durable semantic task plans and recovery packets for root conversation runs."""
# 文件职责：负责后台任务图及状态调度中的 state 子模块。
# 逻辑关系：上层通过 tasks/state.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select, text

from src.persistence import database as database_module
from src.persistence.database import ChatMessage, DurableTask, PlanStep, Run
from src.tasks.graph import dependency_map, replace_dependencies, validate_dependency_graph


# 变量说明：RESUMABLE_TASK_STATUSES 表示当前流程使用的 RESUMABLE_TASK_STATUSES 集合。
RESUMABLE_TASK_STATUSES = frozenset({"running", "waiting", "paused", "needs_recovery", "blocked"})
# 变量说明：_CONTINUATION_PHRASES 表示当前流程使用的 _CONTINUATION_PHRASES 集合。
_CONTINUATION_PHRASES = frozenset({
    "继续", "继续工作", "继续任务", "继续刚刚的工作", "继续之前的工作", "接着做", "恢复任务",
    "continue", "continue the task", "continue the previous task", "resume", "resume task",
})
# 变量说明：_CANCELLATION_PHRASES 表示当前流程使用的 _CANCELLATION_PHRASES 集合。
_CANCELLATION_PHRASES = frozenset({
    "取消任务", "取消这个任务", "取消当前任务", "终止任务", "终止这个任务", "终止当前任务",
    "不要继续这个任务", "不用继续这个任务", "cancel task", "cancel this task", "cancel the task",
})


# 函数职责：完成 utcnow 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 函数职责：完成 is_continuation_request 对应的业务处理。
# 参数关系：content 表示待处理或返回的正文内容。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def is_continuation_request(content: str) -> bool:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
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


# 函数职责：完成 is_task_cancellation_request 对应的业务处理。
# 参数关系：content 表示待处理或返回的正文内容。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def is_task_cancellation_request(content: str) -> bool:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = " ".join(content.strip().casefold().split()).rstrip("。?!？！")
    return normalized in _CANCELLATION_PHRASES


# 函数职责：完成 steps_for_task 对应的业务处理。
# 参数关系：db 表示当前数据库会话；task_id 表示任务标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def steps_for_task(db: Any, task_id: str) -> list[PlanStep]:
    return list(db.scalars(
        select(PlanStep)
        .where(PlanStep.task_id == task_id)
        .order_by(PlanStep.position.asc(), PlanStep.created_at.asc(), PlanStep.id.asc())
    ))


# 函数职责：完成 cancel_durable_task 对应的业务处理。
# 参数关系：db 表示当前数据库会话；task 表示当前步骤使用的 task 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def cancel_durable_task(db: Any, task: DurableTask) -> DurableTask:
    """Make a durable task terminal while retaining its completed evidence."""

    if task.status == "cancelled":
        return task
    # 变量说明：now 表示当前时间。
    now = utcnow()
    for step in steps_for_task(db, task.id):
        if step.status != "completed":
            # 变量说明：status 表示当前对象或运行的状态。
            step.status = "cancelled"
            # 变量说明：next_action 表示当前步骤使用的 next_action 值。
            step.next_action = ""
            # 变量说明：completed_at 表示completed_at 对应的时间信息。
            step.completed_at = now
    # 变量说明：status 表示当前对象或运行的状态。
    task.status = "cancelled"
    # 变量说明：active_step_id 表示active_step 对象的唯一标识。
    task.active_step_id = None
    # 变量说明：completed_at 表示completed_at 对应的时间信息。
    task.completed_at = now
    # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
    task.resume_summary = "用户已取消此任务。"
    return task


# 函数职责：完成 latest_resumable_task 对应的业务处理。
# 参数关系：db 表示当前数据库会话；session_id 表示所属会话标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def latest_resumable_task(db: Any, session_id: str) -> DurableTask | None:
    return db.scalar(
        select(DurableTask)
        .where(DurableTask.session_id == session_id, DurableTask.status.in_(RESUMABLE_TASK_STATUSES))
        .order_by(DurableTask.updated_at.desc(), DurableTask.created_at.desc(), DurableTask.id.desc())
        .limit(1)
    )


# 函数职责：完成 goal_for_run 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _goal_for_run(db: Any, run: Run) -> str:
    if run.turn_id:
        # 变量说明：message 表示当前消息。
        message = db.scalar(select(ChatMessage).where(
            ChatMessage.turn_id == run.turn_id,
            ChatMessage.role == "user",
        ))
        if message is not None and str(message.content or "").strip():
            return str(message.content).strip()
    return "Continue the accepted user task."


# 函数职责：完成 bind_recovery_task 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值；content 表示待处理或返回的正文内容。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def bind_recovery_task(db: Any, run: Run, content: str) -> DurableTask | None:
    """Attach a continuation turn to the latest unfinished task in its session."""

    if not run.session_id or not is_continuation_request(content):
        return None
    # 变量说明：task 表示当前步骤使用的 task 值。
    task = latest_resumable_task(db, run.session_id)
    if task is None:
        return None
    # 变量说明：previous 表示当前流程使用的 previous 集合。
    previous = db.scalar(
        select(Run)
        .where(Run.task_id == task.id, Run.id != run.id)
        .order_by(Run.started_at.desc(), Run.id.desc())
        .limit(1)
    )
    # 变量说明：task_id 表示任务标识。
    run.task_id = task.id
    # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
    run.plan_step_id = task.active_step_id
    # 变量说明：run_kind 表示当前步骤使用的 run_kind 值。
    run.run_kind = "recovery"
    # 变量说明：resumed_from_run_id 表示resumed_from_run 对象的唯一标识。
    run.resumed_from_run_id = previous.id if previous is not None else None
    # 变量说明：status 表示当前对象或运行的状态。
    task.status = "running"
    # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
    task.resume_summary = "用户请求继续未完成任务；新运行将先核验上次活动步骤。"
    return task


# 函数职责：规范化 todos 对应的数据或流程。
# 参数关系：todos 表示当前流程使用的 todos 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _normalize_todos(todos: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(todos, start=1):
        # 变量说明：content 表示待处理或返回的正文内容。
        content = str(raw.get("content") or "").strip()
        if not content:
            continue
        # 变量说明：status 表示当前对象或运行的状态。
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in {"pending", "in_progress", "completed", "cancelled"}:
            # 变量说明：status 表示当前对象或运行的状态。
            status = "pending"
        # 变量说明：external_id 表示external 对象的唯一标识。
        external_id = str(raw.get("id") or "").strip()
        if not external_id:
            raise ValueError(f"todo {index} requires a stable id")
        # 变量说明：raw_dependencies 表示当前流程使用的 raw_dependencies 集合。
        raw_dependencies = raw.get("depends_on", raw.get("blockedBy", []))
        if raw_dependencies is None:
            # 变量说明：raw_dependencies 表示当前流程使用的 raw_dependencies 集合。
            raw_dependencies = []
        if not isinstance(raw_dependencies, (list, tuple)):
            raise ValueError(f"todo {external_id} depends_on must be an array")
        # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
        dependencies = list(dict.fromkeys(
            str(value).strip() for value in raw_dependencies if str(value).strip()
        ))
        # 变量说明：executor_kind 表示当前步骤使用的 executor_kind 值。
        executor_kind = str(raw.get("executor_kind") or raw.get("executor") or "main").strip().lower()
        if executor_kind not in {"main", "subagent", "background"}:
            raise ValueError(f"todo {external_id} has invalid executor_kind")
        # 变量说明：assigned_agent_id 表示assigned_agent 对象的唯一标识。
        assigned_agent_id = str(raw.get("agent_id") or raw.get("assigned_agent_id") or "").strip()
        if executor_kind == "subagent" and not assigned_agent_id:
            raise ValueError(f"todo {external_id} requires agent_id for subagent execution")
        # 变量说明：workspace_mode 表示当前步骤使用的 workspace_mode 值。
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


# 函数职责：完成 sync_todos_for_run 对应的业务处理。
# 参数关系：run_id 表示当前运行标识；todos 表示当前流程使用的 todos 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def sync_todos_for_run(run_id: str, todos: Iterable[Mapping[str, Any]]) -> None:
    """Persist one successful TodoWrite as the run's semantic task plan."""

    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = _normalize_todos(todos)
    with database_module.SessionLocal() as db:
        # TodoWrite executes in a worker thread while stop/outcome persistence
        # may run in another transaction. Claim the SQLite writer slot before
        # reading the Run so commit order also determines lifecycle order.
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
            # 变量说明：run 表示当前步骤使用的 run 值。
            run = db.get(Run, run_id)
        else:
            # 变量说明：run 表示当前步骤使用的 run 值。
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or not run.session_id:
            return
        if run.status in {"completed", "failed", "stopped"}:
            return
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = db.get(DurableTask, run.task_id) if run.task_id else None
        if task is None and not normalized:
            return
        if task is None:
            # 变量说明：task 表示当前步骤使用的 task 值。
            task = DurableTask(
                session_id=run.session_id,
                origin_turn_id=run.turn_id,
                goal=_goal_for_run(db, run),
                status="planning",
            )
            db.add(task)
            db.flush()
            # 变量说明：task_id 表示任务标识。
            run.task_id = task.id

        # 变量说明：existing 表示当前步骤使用的 existing 值。
        existing = {step.external_id: step for step in steps_for_task(db, task.id)}
        # 变量说明：cleared_incomplete_plan 表示当前步骤使用的 cleared_incomplete_plan 值。
        cleared_incomplete_plan = not normalized and any(
            step.status not in {"completed", "cancelled"} for step in existing.values()
        )
        # 变量说明：retained 表示当前步骤使用的 retained 值。
        retained: set[str] = set()
        # 变量说明：now 表示当前时间。
        now = utcnow()
        for position, item in enumerate(normalized, start=1):
            # 变量说明：external_id 表示external 对象的唯一标识。
            external_id = item["id"]
            retained.add(external_id)
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = existing.get(external_id)
            if step is None:
                # 变量说明：step 表示当前步骤使用的 step 值。
                step = PlanStep(task_id=task.id, external_id=external_id, position=position, title=item["content"])
                db.add(step)
            # 变量说明：previous_status 表示当前流程使用的 previous_status 集合。
            previous_status = step.status
            # 变量说明：position 表示当前步骤使用的 position 值。
            step.position = position
            # 变量说明：title 表示当前步骤使用的 title 值。
            step.title = item["content"]
            # 变量说明：description 表示当前步骤使用的 description 值。
            step.description = item["active_form"]
            # 变量说明：requested_status 表示当前流程使用的 requested_status 集合。
            requested_status = item["status"]
            # 子 Agent 步骤由 task 委派动作负责领取；计划工具可能会沿用主 Agent
            # 的“当前进行中”习惯，但在真正委派前不能把该步骤标成已被占用。
            # 已经存在 assigned_run_id 的步骤属于真实子运行，不回退其运行状态。
            if (
                item["executor_kind"] == "subagent"
                and requested_status == "in_progress"
                and step.assigned_run_id is None
            ):
                requested_status = "pending"
            # 变量说明：status 表示当前对象或运行的状态。
            step.status = (
                "completed"
                if previous_status == "completed" and requested_status != "completed"
                else requested_status
            )
            # 变量说明：executor_kind 表示当前步骤使用的 executor_kind 值。
            step.executor_kind = item["executor_kind"]
            # 变量说明：assigned_agent_id 表示assigned_agent 对象的唯一标识。
            step.assigned_agent_id = item["agent_id"] or None
            # 变量说明：workspace_mode 表示当前步骤使用的 workspace_mode 值。
            step.workspace_mode = item["workspace_mode"]
            # 变量说明：last_run_id 表示last_run 对象的唯一标识。
            step.last_run_id = run.id
            if step.status == "in_progress":
                # 变量说明：started_at 表示started_at 对应的时间信息。
                step.started_at = step.started_at or now
                # 变量说明：next_action 表示当前步骤使用的 next_action 值。
                step.next_action = item["active_form"] or item["content"]
                # 变量说明：remaining_work 表示当前步骤使用的 remaining_work 值。
                step.remaining_work = [item["content"]]
            elif step.status == "completed":
                # 变量说明：started_at 表示started_at 对应的时间信息。
                step.started_at = step.started_at or now
                # 变量说明：completed_at 表示completed_at 对应的时间信息。
                step.completed_at = step.completed_at or now
                # 变量说明：completed_work 表示当前步骤使用的 completed_work 值。
                step.completed_work = [item["content"]]
                # 变量说明：remaining_work 表示当前步骤使用的 remaining_work 值。
                step.remaining_work = []
                # 变量说明：next_action 表示当前步骤使用的 next_action 值。
                step.next_action = ""
                if not step.result:
                    # 变量说明：result 表示本步骤产生的结果。
                    step.result = item["content"]
            elif step.status == "pending":
                # 变量说明：remaining_work 表示当前步骤使用的 remaining_work 值。
                step.remaining_work = [item["content"]]
                # 变量说明：next_action 表示当前步骤使用的 next_action 值。
                step.next_action = ""
                if previous_status == "needs_recovery":
                    # 变量说明：result 表示本步骤产生的结果。
                    step.result = "恢复核验后确认该步骤尚未开始。"
            else:
                # 变量说明：next_action 表示当前步骤使用的 next_action 值。
                step.next_action = ""

        for external_id, step in existing.items():
            if external_id not in retained and step.status != "completed":
                # 变量说明：status 表示当前对象或运行的状态。
                step.status = "cancelled"
                # 变量说明：next_action 表示当前步骤使用的 next_action 值。
                step.next_action = ""

        db.flush()
        replace_dependencies(
            db,
            task_id=task.id,
            items=normalized,
            steps_by_external_id={step.external_id: step for step in steps_for_task(db, task.id)},
        )
        db.flush()
        # 变量说明：steps 表示当前流程使用的 steps 集合。
        steps = steps_for_task(db, task.id)
        if not normalized:
            # 变量说明：active_step_id 表示active_step 对象的唯一标识。
            task.active_step_id = None
            # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
            run.plan_step_id = None
            # 变量说明：status 表示当前对象或运行的状态。
            task.status = "cancelled" if cleared_incomplete_plan else "completed"
            # 变量说明：completed_at 表示completed_at 对应的时间信息。
            task.completed_at = now
            # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
            task.resume_summary = (
                "计划已清空；原有未完成步骤已取消。"
                if cleared_incomplete_plan else "计划中的步骤均已完成。"
            )
            db.commit()
            return
        # 变量说明：active 表示当前步骤使用的 active 值。
        active = next((step for step in steps if step.status == "in_progress"), None)
        # 变量说明：active 表示当前步骤使用的 active 值。
        active = active or next((step for step in steps if step.status == "needs_recovery"), None)
        # 变量说明：active 表示当前步骤使用的 active 值。
        active = active or next((step for step in steps if step.status == "pending"), None)
        # 变量说明：active_step_id 表示active_step 对象的唯一标识。
        task.active_step_id = active.id if active is not None else None
        # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
        run.plan_step_id = task.active_step_id
        if steps and all(step.status in {"completed", "cancelled"} for step in steps):
            # 变量说明：status 表示当前对象或运行的状态。
            task.status = "completed"
            # 变量说明：completed_at 表示completed_at 对应的时间信息。
            task.completed_at = now
            # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
            task.resume_summary = "计划中的步骤均已完成。"
        else:
            # 变量说明：status 表示当前对象或运行的状态。
            task.status = "running"
            # 变量说明：completed_at 表示completed_at 对应的时间信息。
            task.completed_at = None
            # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
            task.resume_summary = (
                f"当前步骤：{active.title}；下一动作：{active.next_action or active.title}"
                if active is not None else "计划已创建，等待下一步。"
            )
        db.commit()


# 函数职责：完成 todo_state_for_run 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def todo_state_for_run(db: Any, run: Run) -> list[dict[str, Any]]:
    if not run.task_id:
        return []
    # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
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


# 函数职责：完成 task_checkpoint_for_run 对应的业务处理。
# 参数关系：run_id 表示当前运行标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def task_checkpoint_for_run(run_id: str) -> dict[str, Any]:
    """Read the latest durable task facts for compaction from a fresh transaction."""

    with database_module.SessionLocal() as db:
        # 变量说明：run 表示当前步骤使用的 run 值。
        run = db.get(Run, run_id)
        if run is None or not run.task_id:
            return {}
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = db.get(DurableTask, run.task_id)
        if task is None:
            return {}
        return json.loads(json.dumps(task_payload(db, task), ensure_ascii=False, default=str))


# 函数职责：完成 recovery_prompt 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def recovery_prompt(db: Any, run: Run) -> str:
    if not run.task_id:
        return ""
    # 变量说明：task 表示当前步骤使用的 task 值。
    task = db.get(DurableTask, run.task_id)
    if task is None:
        return ""
    # 变量说明：steps 表示当前流程使用的 steps 集合。
    steps = steps_for_task(db, task.id)
    # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
    dependencies = dependency_map(db, task.id)
    # 变量说明：packet 表示当前步骤使用的 packet 值。
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


# 函数职责：完成 transition_run_task 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值；status 表示当前对象或运行的状态；stop_reason 表示当前步骤使用的 stop_reason 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def transition_run_task(db: Any, run: Run, *, status: str, stop_reason: str | None = None) -> None:
    """Project a run terminal/interruption state onto its durable task."""

    if not run.task_id:
        return
    # 变量说明：task 表示当前步骤使用的 task 值。
    task = db.get(DurableTask, run.task_id)
    if task is None:
        return
    # 变量说明：steps 表示当前流程使用的 steps 集合。
    steps = steps_for_task(db, task.id)
    # 变量说明：active 表示当前步骤使用的 active 值。
    active = db.get(PlanStep, task.active_step_id) if task.active_step_id else None
    # 变量说明：now 表示当前时间。
    now = utcnow()
    if status == "completed" and steps and all(step.status in {"completed", "cancelled"} for step in steps):
        # 变量说明：status 表示当前对象或运行的状态。
        task.status = "completed"
        # 变量说明：completed_at 表示completed_at 对应的时间信息。
        task.completed_at = now
        # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
        task.resume_summary = "计划中的步骤均已完成。"
        return
    if status == "awaiting_approval":
        return
    if status == "stopped" and stop_reason == "waiting_background":
        # 变量说明：status 表示当前对象或运行的状态。
        task.status = "waiting"
        # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
        task.resume_summary = "Waiting for a background task terminal event; the coordinator will resume automatically."
        return
    if status == "stopped" and stop_reason in {
        "delegated_child_awaiting_approval",
        "delegated_child_waiting_event",
    }:
        # 变量说明：status 表示当前对象或运行的状态。
        task.status = "waiting"
        # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
        task.resume_summary = "Waiting for a delegated child event before the coordinator continues."
        return
    if status == "stopped" and stop_reason == "user_interrupted":
        # 变量说明：status 表示当前对象或运行的状态。
        task.status = "paused"
        # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
        task.resume_summary = "用户暂停了任务；恢复时先核验活动步骤是否产生了部分结果。"
    elif status in {"failed", "stopped"}:
        # 变量说明：status 表示当前对象或运行的状态。
        task.status = "needs_recovery"
        # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
        task.resume_summary = f"运行中断（{stop_reason or status}）；活动步骤结果尚待核验。"
    elif status == "completed":
        # 变量说明：status 表示当前对象或运行的状态。
        task.status = "paused"
        # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
        task.resume_summary = "本轮运行已经结束，但持久化计划仍有未完成步骤。"
    if active is not None and active.status == "in_progress" and status in {"failed", "stopped"}:
        # 变量说明：status 表示当前对象或运行的状态。
        active.status = "needs_recovery"
        # 变量说明：next_action 表示当前步骤使用的 next_action 值。
        active.next_action = f"核验“{active.title}”已经产生的实际结果，再决定补做或完成。"
        # 变量说明：last_run_id 表示last_run 对象的唯一标识。
        active.last_run_id = run.id


# 函数职责：完成 task_payload 对应的业务处理。
# 参数关系：db 表示当前数据库会话；task 表示当前步骤使用的 task 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def task_payload(db: Any, task: DurableTask) -> dict[str, Any]:
    # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
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
