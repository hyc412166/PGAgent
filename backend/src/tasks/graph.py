"""Dependency-aware scheduling for durable task plan steps."""
# 文件职责：维护任务节点及依赖边，计算可运行节点、阻塞原因和图级完成状态。
# 逻辑关系：background 调度器读取任务图选择依赖已满足的节点；节点执行结果回写 state 后再次推进图，最终由运行服务汇总为用户可见状态。

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import delete, select, text

from src.persistence import database as database_module
from src.persistence.database import ChatMessage, DurableTask, PlanStep, PlanStepDependency, Run
from src.tools.types import ToolResult


# 变量说明：TERMINAL_STEP_STATUSES 表示当前流程使用的 TERMINAL_STEP_STATUSES 集合。
TERMINAL_STEP_STATUSES = frozenset({"completed", "cancelled", "failed"})
# 变量说明：SUCCESSFUL_DEPENDENCY_STATUSES 表示当前流程使用的 SUCCESSFUL_DEPENDENCY_STATUSES 集合。
SUCCESSFUL_DEPENDENCY_STATUSES = frozenset({"completed"})


# 函数职责：完成 utcnow 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    return "Complete the durable task graph."


# 函数职责：校验 dependency_graph 对应的数据或流程。
# 参数关系：items 表示待处理的元素集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def validate_dependency_graph(items: Iterable[Mapping[str, Any]]) -> None:
    """Reject missing, self-referential, and cyclic prerequisite edges."""

    # 变量说明：rows 表示当前流程使用的 rows 集合。
    rows = [dict(item) for item in items]
    # 变量说明：identifiers 表示当前流程使用的 identifiers 集合。
    identifiers = [str(item.get("id") or "").strip() for item in rows]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("task step ids must be unique")
    # 变量说明：known 表示当前步骤使用的 known 值。
    known = set(identifiers)
    # 变量说明：graph 表示当前步骤使用的 graph 值。
    graph: dict[str, tuple[str, ...]] = {}
    for item, step_id in zip(rows, identifiers, strict=True):
        # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
        dependencies = tuple(str(value).strip() for value in item.get("depends_on") or ())
        # 变量说明：missing 表示当前步骤使用的 missing 值。
        missing = [value for value in dependencies if value not in known]
        if missing:
            raise ValueError(f"step {step_id} depends on unknown step: {missing[0]}")
        if step_id in dependencies:
            raise ValueError(f"step {step_id} cannot depend on itself")
        # 变量说明：graph 的索引项 表示该语句创建或更新的目标数据。
        graph[step_id] = dependencies

    # 变量说明：visiting 表示当前步骤使用的 visiting 值。
    visiting: set[str] = set()
    # 变量说明：visited 表示当前步骤使用的 visited 值。
    visited: set[str] = set()

    # 函数职责：完成 visit 对应的业务处理。
    # 参数关系：step_id 表示step 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：完成 replace_dependencies 对应的业务处理。
# 参数关系：db 表示当前数据库会话；task_id 表示任务标识；items 表示待处理的元素集合；steps_by_external_id 表示steps_by_external 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def replace_dependencies(
    db: Any,
    *,
    task_id: str,
    items: Iterable[Mapping[str, Any]],
    steps_by_external_id: Mapping[str, PlanStep],
) -> None:
    """Replace the dependency edges for the currently retained task steps."""

    # 变量说明：rows 表示当前流程使用的 rows 集合。
    rows = [dict(item) for item in items]
    validate_dependency_graph(rows)
    # 变量说明：task_step_ids 表示task_step 对象标识集合。
    task_step_ids = list(db.scalars(select(PlanStep.id).where(PlanStep.task_id == task_id)))
    if task_step_ids:
        db.execute(delete(PlanStepDependency).where(PlanStepDependency.step_id.in_(task_step_ids)))
    for item in rows:
        # 变量说明：step 表示当前步骤使用的 step 值。
        step = steps_by_external_id[str(item["id"])]
        for dependency_external_id in item.get("depends_on") or ():
            # 变量说明：dependency 表示当前步骤使用的 dependency 值。
            dependency = steps_by_external_id[str(dependency_external_id)]
            db.add(PlanStepDependency(step_id=step.id, depends_on_step_id=dependency.id))


# 函数职责：完成 upsert_delegated_graph 对应的业务处理。
# 参数关系：parent_run_id 表示parent_run 对象的唯一标识；specs 表示当前流程使用的 specs 集合；graph_call_id 表示graph_call 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def upsert_delegated_graph(
    parent_run_id: str,
    specs: Iterable[Mapping[str, Any]],
    *,
    graph_call_id: str | None = None,
) -> dict[str, str]:
    """Attach one explicit delegated DAG to the parent's durable task."""

    # 变量说明：rows 表示当前流程使用的 rows 集合。
    rows = [dict(spec) for spec in specs]
    validate_dependency_graph(rows)
    with database_module.SessionLocal() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
            # 变量说明：run 表示当前步骤使用的 run 值。
            run = db.get(Run, parent_run_id)
        else:
            # 变量说明：run 表示当前步骤使用的 run 值。
            run = db.scalar(select(Run).where(Run.id == parent_run_id).with_for_update())
        if run is None or not run.session_id:
            raise ValueError("parent run does not exist or has no conversation")
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = db.get(DurableTask, run.task_id) if run.task_id else None
        if task is None:
            # 变量说明：user_goal 表示当前步骤使用的 user_goal 值。
            user_goal = "Complete the delegated task graph."
            if run.turn_id:
                # 变量说明：message 表示当前消息。
                message = db.scalar(select(ChatMessage).where(
                    ChatMessage.turn_id == run.turn_id,
                    ChatMessage.role == "user",
                ))
                if message is not None and str(message.content or "").strip():
                    # 变量说明：user_goal 表示当前步骤使用的 user_goal 值。
                    user_goal = str(message.content).strip()
            # 变量说明：task 表示当前步骤使用的 task 值。
            task = DurableTask(
                session_id=run.session_id,
                origin_turn_id=run.turn_id,
                goal=user_goal,
                status="running",
            )
            db.add(task)
            db.flush()
            # 变量说明：task_id 表示任务标识。
            run.task_id = task.id

        # 变量说明：existing_steps 表示当前流程使用的 existing_steps 集合。
        existing_steps = list(db.scalars(
            select(PlanStep)
            .where(PlanStep.task_id == task.id)
            .order_by(PlanStep.position.asc(), PlanStep.created_at.asc(), PlanStep.id.asc())
        ))
        # 变量说明：by_external_id 表示by_external 对象的唯一标识。
        by_external_id = {step.external_id: step for step in existing_steps}
        # 变量说明：next_position 表示当前步骤使用的 next_position 值。
        next_position = max((step.position for step in existing_steps), default=0) + 1
        # 变量说明：selected 表示当前步骤使用的 selected 值。
        selected: dict[str, PlanStep] = {}
        # 变量说明：linked_existing_step_ids 表示linked_existing_step 对象标识集合。
        linked_existing_step_ids: set[str] = set()
        # 变量说明：graph_prefix 表示当前步骤使用的 graph_prefix 值。
        graph_prefix = f"delegate:{graph_call_id}:" if graph_call_id else ""
        for spec in rows:
            # 变量说明：local_external_id 表示local_external 对象的唯一标识。
            local_external_id = str(spec["id"])
            # 变量说明：persisted_external_id 表示persisted_external 对象的唯一标识。
            persisted_external_id = f"{graph_prefix}{local_external_id}"
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = by_external_id.get(persisted_external_id)
            # 变量说明：explicit_step 表示当前步骤使用的 explicit_step 值。
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
                # 变量说明：step 表示当前步骤使用的 step 值。
                step = explicit_step
                linked_existing_step_ids.add(step.id)
            if step is None and bool(spec.get("link_existing")):
                # 变量说明：candidates 表示当前流程使用的 candidates 集合。
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
                    # 变量说明：delegated_title 表示当前步骤使用的 delegated_title 值。
                    delegated_title = str(spec.get("task") or "").strip()
                    # 变量说明：exact_matches 表示当前流程使用的 exact_matches 集合。
                    exact_matches = [
                        candidate
                        for candidate in candidates
                        if delegated_title in {
                            str(candidate.title or "").strip(),
                            str(candidate.description or "").strip(),
                        }
                    ]
                    # 变量说明：candidates 表示当前流程使用的 candidates 集合。
                    candidates = exact_matches
                if len(candidates) == 1:
                    # 变量说明：step 表示当前步骤使用的 step 值。
                    step = candidates[0]
                    linked_existing_step_ids.add(step.id)
            if step is None:
                # 变量说明：step 表示当前步骤使用的 step 值。
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
                # 变量说明：by_external_id 的索引项 表示该语句创建或更新的目标数据。
                by_external_id[persisted_external_id] = step
            if step.id not in linked_existing_step_ids:
                # 变量说明：title 表示当前步骤使用的 title 值。
                step.title = str(spec.get("task") or step.title)
            # 变量说明：description 表示当前步骤使用的 description 值。
            step.description = str(spec.get("task") or step.description)
            # 变量说明：executor_kind 表示当前步骤使用的 executor_kind 值。
            step.executor_kind = "subagent"
            # 变量说明：assigned_agent_id 表示assigned_agent 对象的唯一标识。
            step.assigned_agent_id = str(spec.get("agent_id") or "") or None
            # 变量说明：workspace_mode 表示当前步骤使用的 workspace_mode 值。
            step.workspace_mode = str(spec.get("workspace_mode") or "shared")
            # 变量说明：remaining_work 表示当前步骤使用的 remaining_work 值。
            step.remaining_work = [] if step.status == "completed" else [step.title]
            selected[local_external_id] = step

        # 变量说明：selected_step_ids 表示selected_step 对象标识集合。
        selected_step_ids = [
            step.id for step in selected.values() if step.id not in linked_existing_step_ids
        ]
        if selected_step_ids:
            db.execute(delete(PlanStepDependency).where(
                PlanStepDependency.step_id.in_(selected_step_ids)
            ))
        for spec in rows:
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = selected[str(spec["id"])]
            if step.id in linked_existing_step_ids:
                continue
            for dependency_external_id in spec.get("depends_on") or ():
                # 变量说明：dependency 表示当前步骤使用的 dependency 值。
                dependency = selected[str(dependency_external_id)]
                db.add(PlanStepDependency(step_id=step.id, depends_on_step_id=dependency.id))
        db.flush()
        refresh_task_state(db, task.id)
        # 变量说明：first_ready 表示当前步骤使用的 first_ready 值。
        first_ready = next(iter(ready_steps(db, task.id)), None)
        # 变量说明：active_step_id 表示active_step 对象的唯一标识。
        task.active_step_id = first_ready.id if first_ready is not None else task.active_step_id
        # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
        run.plan_step_id = task.active_step_id
        db.commit()
        return {external_id: step.id for external_id, step in selected.items()}


# 函数职责：完成 dependency_map 对应的业务处理。
# 参数关系：db 表示当前数据库会话；task_id 表示任务标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def dependency_map(db: Any, task_id: str) -> dict[str, list[str]]:
    # 变量说明：steps 表示当前流程使用的 steps 集合。
    steps = list(db.scalars(select(PlanStep).where(PlanStep.task_id == task_id)))
    # 变量说明：external_by_id 表示external_by 对象的唯一标识。
    external_by_id = {step.id: step.external_id for step in steps}
    # 变量说明：result 表示本步骤产生的结果。
    result = {step.external_id: [] for step in steps}
    if not steps:
        return result
    # 变量说明：edges 表示当前流程使用的 edges 集合。
    edges = list(db.scalars(
        select(PlanStepDependency).where(PlanStepDependency.step_id.in_(external_by_id))
    ))
    for edge in edges:
        # 变量说明：step_external_id 表示step_external 对象的唯一标识。
        step_external_id = external_by_id.get(edge.step_id)
        # 变量说明：dependency_external_id 表示dependency_external 对象的唯一标识。
        dependency_external_id = external_by_id.get(edge.depends_on_step_id)
        if step_external_id and dependency_external_id:
            result[step_external_id].append(dependency_external_id)
    for dependencies in result.values():
        dependencies.sort()
    return result


# 函数职责：完成 ready_steps 对应的业务处理。
# 参数关系：db 表示当前数据库会话；task_id 表示任务标识；executor_kind 表示当前步骤使用的 executor_kind 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def ready_steps(db: Any, task_id: str, *, executor_kind: str | None = None) -> list[PlanStep]:
    """Return pending steps whose prerequisites have all completed."""

    # 变量说明：steps 表示当前流程使用的 steps 集合。
    steps = list(db.scalars(
        select(PlanStep)
        .where(PlanStep.task_id == task_id)
        .order_by(PlanStep.position.asc(), PlanStep.created_at.asc(), PlanStep.id.asc())
    ))
    # 变量说明：by_id 表示by 对象的唯一标识。
    by_id = {step.id: step for step in steps}
    # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
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


# 函数职责：完成 claim_step 对应的业务处理。
# 参数关系：step_id 表示step 对象的唯一标识；assigned_run_id 表示assigned_run 对象的唯一标识；assigned_agent_id 表示assigned_agent 对象的唯一标识；claim_owner 表示当前步骤使用的 claim_owner 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = db.get(PlanStep, step_id)
        else:
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = db.scalar(select(PlanStep).where(PlanStep.id == step_id).with_for_update())
        if step is None or step.status != "pending":
            db.rollback()
            return False
        # 变量说明：dependency_ids 表示dependency 对象标识集合。
        dependency_ids = list(db.scalars(
            select(PlanStepDependency.depends_on_step_id).where(PlanStepDependency.step_id == step.id)
        ))
        if dependency_ids:
            # 变量说明：statuses 表示当前流程使用的 statuses 集合。
            statuses = list(db.scalars(select(PlanStep.status).where(PlanStep.id.in_(dependency_ids))))
            if len(statuses) != len(dependency_ids) or any(
                status not in SUCCESSFUL_DEPENDENCY_STATUSES for status in statuses
            ):
                db.rollback()
                return False
        # 变量说明：status 表示当前对象或运行的状态。
        step.status = "in_progress"
        # 变量说明：started_at 表示started_at 对应的时间信息。
        step.started_at = step.started_at or utcnow()
        # 变量说明：assigned_run_id 表示assigned_run 对象的唯一标识。
        step.assigned_run_id = assigned_run_id
        # 变量说明：assigned_agent_id 表示assigned_agent 对象的唯一标识。
        step.assigned_agent_id = assigned_agent_id or step.assigned_agent_id
        # 变量说明：claim_owner 表示当前步骤使用的 claim_owner 值。
        step.claim_owner = claim_owner or step.claim_owner
        # 变量说明：attempt 表示当前步骤使用的 attempt 值。
        step.attempt = int(step.attempt or 0) + 1
        # 变量说明：error 表示当前捕获或准备上报的错误。
        step.error = None
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = db.get(DurableTask, step.task_id)
        if task is not None:
            # 变量说明：status 表示当前对象或运行的状态。
            task.status = "running"
            # 变量说明：active_step_id 表示active_step 对象的唯一标识。
            task.active_step_id = step.id
        db.commit()
        return True


# 类职责：封装 TaskGraphToolStore 的持久化访问。
class TaskGraphToolStore:
    """Compatibility task-board tools backed by the canonical plan-step DAG."""

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self, *, run_id: str) -> None:
        # 变量说明：run_id 表示当前运行标识。
        self.run_id = run_id

    # 函数职责：完成 task 对应的业务处理。
    # 参数关系：db 表示当前数据库会话。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _task(self, db: Any) -> DurableTask:
        # 变量说明：run 表示当前步骤使用的 run 值。
        run = db.get(Run, self.run_id)
        if run is None or not run.session_id:
            raise ValueError("parent run does not exist or has no conversation")
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = db.get(DurableTask, run.task_id) if run.task_id else None
        if task is None:
            # 变量说明：task 表示当前步骤使用的 task 值。
            task = DurableTask(
                session_id=run.session_id,
                origin_turn_id=run.turn_id,
                goal=_goal_for_run(db, run),
                status="running",
            )
            db.add(task)
            db.flush()
            # 变量说明：task_id 表示任务标识。
            run.task_id = task.id
        return task

    # 函数职责：完成 step 对应的业务处理。
    # 参数关系：db 表示当前数据库会话；task_id 表示任务标识；task_id_value 表示当前步骤使用的 task_id_value 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _step(db: Any, task_id: str, task_id_value: str | int) -> PlanStep | None:
        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized = str(task_id_value)
        # 变量说明：step 表示当前步骤使用的 step 值。
        step = db.get(PlanStep, normalized)
        if step is not None and step.task_id == task_id:
            return step
        return db.scalar(select(PlanStep).where(
            PlanStep.task_id == task_id,
            PlanStep.external_id == normalized,
        ))

    # 函数职责：完成 payload 对应的业务处理。
    # 参数关系：db 表示当前数据库会话；step 表示当前步骤使用的 step 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _payload(db: Any, step: PlanStep) -> dict[str, Any]:
        # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
        dependencies = dependency_map(db, step.task_id).get(step.external_id, [])
        # 变量说明：messages 表示发送给模型或客户端的消息序列。
        messages = [
            item
            for item in list(step.evidence or [])
            if isinstance(item, dict) and item.get("type") == "message"
        ]
        # 变量说明：packet 表示当前步骤使用的 packet 值。
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

    # 函数职责：完成 create 对应的业务处理。
    # 参数关系：subject 表示当前步骤使用的 subject 值；description 表示当前步骤使用的 description 值；prompt 表示当前步骤使用的 prompt 值；packet 表示当前步骤使用的 packet 值；tool_name 表示当前步骤使用的 tool_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def create(
        self,
        *,
        subject: str | None = None,
        description: str = "",
        prompt: str | None = None,
        packet: Mapping[str, Any] | None = None,
        tool_name: str = "task_create",
    ) -> ToolResult:
        # 变量说明：title 表示当前步骤使用的 title 值。
        title = str(subject or description or prompt or (packet or {}).get("objective") or "").strip()
        if not title:
            return ToolResult(tool_name, False, "task title cannot be empty", error_code="invalid_arguments")
        with database_module.SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            # 变量说明：task 表示当前步骤使用的 task 值。
            task = self._task(db)
            # 变量说明：existing 表示当前步骤使用的 existing 值。
            existing = list(db.scalars(select(PlanStep).where(PlanStep.task_id == task.id)))
            # 变量说明：numeric_ids 表示numeric 对象标识集合。
            numeric_ids = [int(step.external_id) for step in existing if step.external_id.isdigit()]
            # 变量说明：position 表示当前步骤使用的 position 值。
            position = max((step.position for step in existing), default=0) + 1
            # 变量说明：evidence 表示当前步骤使用的 evidence 值。
            evidence = ([{"type": "task_packet", "packet": dict(packet)}] if packet else [])
            # 变量说明：step 表示当前步骤使用的 step 值。
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
            # 变量说明：payload 表示跨层传递的数据载荷。
            payload = self._payload(db, step)
            db.commit()
            return ToolResult(tool_name, True, json.dumps(payload, ensure_ascii=False), changed=True)

    # 函数职责：完成 get 对应的业务处理。
    # 参数关系：task_id 表示任务标识；tool_name 表示当前步骤使用的 tool_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def get(self, *, task_id: str | int, tool_name: str = "task_get") -> ToolResult:
        with database_module.SessionLocal() as db:
            # 变量说明：task 表示当前步骤使用的 task 值。
            task = self._task(db)
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = self._step(db, task.id, task_id)
            if step is None:
                return ToolResult(tool_name, False, "task does not exist", error_code="task_not_found")
            return ToolResult(tool_name, True, json.dumps(self._payload(db, step), ensure_ascii=False))

    # 函数职责：完成 list 对应的业务处理。
    # 参数关系：tool_name 表示当前步骤使用的 tool_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def list(self, *, tool_name: str = "task_list") -> ToolResult:
        with database_module.SessionLocal() as db:
            # 变量说明：task 表示当前步骤使用的 task 值。
            task = self._task(db)
            # 变量说明：steps 表示当前流程使用的 steps 集合。
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

    # 函数职责：完成 update 对应的业务处理。
    # 参数关系：task_id 表示任务标识；status 表示当前对象或运行的状态；message 表示当前消息；add_blocked_by 表示当前步骤使用的 add_blocked_by 值；remove_blocked_by 表示当前步骤使用的 remove_blocked_by 值；output 表示当前步骤使用的 output 值；tool_name 表示当前步骤使用的 tool_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
        # 变量说明：status_map 表示按键快速定位 status_map 数据的映射。
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
            # 变量说明：task 表示当前步骤使用的 task 值。
            task = self._task(db)
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = self._step(db, task.id, task_id)
            if step is None:
                db.rollback()
                return ToolResult(tool_name, False, "task does not exist", error_code="task_not_found")
            # 变量说明：steps 表示当前流程使用的 steps 集合。
            steps = list(db.scalars(select(PlanStep).where(PlanStep.task_id == task.id)))
            # 变量说明：by_external_id 表示by_external 对象的唯一标识。
            by_external_id = {item.external_id: item for item in steps}
            # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
            dependencies = dependency_map(db, task.id)
            # 变量说明：blocked 表示当前步骤使用的 blocked 值。
            blocked = set(dependencies.get(step.external_id, []))
            blocked.update(str(item) for item in add_blocked_by or ())
            blocked.difference_update(str(item) for item in remove_blocked_by or ())
            if any(item not in by_external_id for item in blocked):
                db.rollback()
                return ToolResult(tool_name, False, "dependency task does not exist", error_code="task_not_found")
            # 变量说明：dependencies 的索引项 表示该语句创建或更新的目标数据。
            dependencies[step.external_id] = sorted(blocked)
            # 变量说明：graph_rows 表示当前流程使用的 graph_rows 集合。
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
                # 变量说明：status 表示当前对象或运行的状态。
                step.status = status_map[status]
                if step.status == "in_progress":
                    # 变量说明：started_at 表示started_at 对应的时间信息。
                    step.started_at = step.started_at or utcnow()
                    # 变量说明：attempt 表示当前步骤使用的 attempt 值。
                    step.attempt = int(step.attempt or 0) + 1
                if step.status in TERMINAL_STEP_STATUSES:
                    # 变量说明：completed_at 表示completed_at 对应的时间信息。
                    step.completed_at = utcnow()
                    # 变量说明：remaining_work 表示当前步骤使用的 remaining_work 值。
                    step.remaining_work = [] if step.status == "completed" else list(step.remaining_work or [step.title])
                    if step.status == "completed":
                        # 变量说明：completed_work 表示当前步骤使用的 completed_work 值。
                        step.completed_work = list(step.completed_work or [step.title])
                elif step.status == "pending":
                    # 变量说明：completed_at 表示completed_at 对应的时间信息。
                    step.completed_at = None
            if message:
                # 变量说明：evidence 表示当前步骤使用的 evidence 值。
                step.evidence = [
                    *list(step.evidence or []),
                    {"type": "message", "at": utcnow().isoformat(), "message": str(message)},
                ]
            if output is not None:
                # 变量说明：result 表示本步骤产生的结果。
                step.result = str(output)
            refresh_task_state(db, task.id)
            # 变量说明：payload 表示跨层传递的数据载荷。
            payload = self._payload(db, step)
            db.commit()
            return ToolResult(tool_name, True, json.dumps(payload, ensure_ascii=False), changed=True)

    # 函数职责：完成 claim 对应的业务处理。
    # 参数关系：task_id 表示任务标识；owner 表示当前步骤使用的 owner 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def claim(self, *, task_id: str | int, owner: str = "lead") -> ToolResult:
        with database_module.SessionLocal() as db:
            # 变量说明：task 表示当前步骤使用的 task 值。
            task = self._task(db)
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = self._step(db, task.id, task_id)
            if step is None:
                return ToolResult("claim_task", False, "task does not exist", error_code="task_not_found")
            # 变量说明：step_id 表示step 对象的唯一标识。
            step_id = step.id
            # 变量说明：existing_owner 表示当前步骤使用的 existing_owner 值。
            existing_owner = step.claim_owner
        # 变量说明：normalized_owner 表示当前步骤使用的 normalized_owner 值。
        normalized_owner = str(owner or "lead")[:160]
        if existing_owner not in {None, "", normalized_owner}:
            return ToolResult("claim_task", False, f"task is already claimed by {existing_owner}", error_code="task_claimed")
        if not claim_step(step_id, assigned_run_id=self.run_id, claim_owner=normalized_owner):
            return ToolResult("claim_task", False, "task is not ready to claim", error_code="task_blocked")
        with database_module.SessionLocal() as db:
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = db.get(PlanStep, step_id)
            return ToolResult(
                "claim_task", True, json.dumps(self._payload(db, step), ensure_ascii=False), changed=True
            )


# 函数职责：完成 refresh_task_state 对应的业务处理。
# 参数关系：db 表示当前数据库会话；task_id 表示任务标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def refresh_task_state(db: Any, task_id: str) -> DurableTask | None:
    # 变量说明：task 表示当前步骤使用的 task 值。
    task = db.get(DurableTask, task_id)
    if task is None:
        return None
    # 变量说明：steps 表示当前流程使用的 steps 集合。
    steps = list(db.scalars(
        select(PlanStep)
        .where(PlanStep.task_id == task_id)
        .order_by(PlanStep.position.asc(), PlanStep.created_at.asc(), PlanStep.id.asc())
    ))
    # 变量说明：now 表示当前时间。
    now = utcnow()
    if steps and all(step.status in {"completed", "cancelled"} for step in steps):
        # 变量说明：status 表示当前对象或运行的状态。
        task.status = "completed"
        # 变量说明：active_step_id 表示active_step 对象的唯一标识。
        task.active_step_id = None
        # 变量说明：completed_at 表示completed_at 对应的时间信息。
        task.completed_at = now
        # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
        task.resume_summary = "All task graph steps reached a terminal accepted state."
        return task
    # 变量说明：failed 表示当前步骤使用的 failed 值。
    failed = next((step for step in steps if step.status == "failed"), None)
    if failed is not None:
        # 变量说明：status 表示当前对象或运行的状态。
        task.status = "blocked"
        # 变量说明：active_step_id 表示active_step 对象的唯一标识。
        task.active_step_id = failed.id
        # 变量说明：completed_at 表示completed_at 对应的时间信息。
        task.completed_at = None
        # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
        task.resume_summary = f"Step {failed.external_id} failed: {failed.error or failed.title}"
        return task
    # 变量说明：active 表示当前步骤使用的 active 值。
    active = next((step for step in steps if step.status in {"in_progress", "needs_recovery"}), None)
    # 变量说明：active 表示当前步骤使用的 active 值。
    active = active or next(iter(ready_steps(db, task_id)), None)
    # 变量说明：status 表示当前对象或运行的状态。
    task.status = "running"
    # 变量说明：active_step_id 表示active_step 对象的唯一标识。
    task.active_step_id = active.id if active is not None else None
    # 变量说明：completed_at 表示completed_at 对应的时间信息。
    task.completed_at = None
    # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
    task.resume_summary = (
        f"Current step: {active.title}" if active is not None else "Pending steps are waiting for prerequisites."
    )
    return task


# 函数职责：完成 settle_step 对应的业务处理。
# 参数关系：step_id 表示step 对象的唯一标识；status 表示当前对象或运行的状态；result 表示本步骤产生的结果；error 表示当前捕获或准备上报的错误；evidence 表示当前步骤使用的 evidence 值；assigned_run_id 表示assigned_run 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = db.get(PlanStep, step_id)
        else:
            # 变量说明：step 表示当前步骤使用的 step 值。
            step = db.scalar(select(PlanStep).where(PlanStep.id == step_id).with_for_update())
        if step is None or step.status not in {"in_progress", "needs_recovery", "pending"}:
            db.rollback()
            return False
        # 变量说明：status 表示当前对象或运行的状态。
        step.status = status
        # 变量说明：result 表示本步骤产生的结果。
        step.result = str(result or "")
        # 变量说明：error 表示当前捕获或准备上报的错误。
        step.error = str(error) if error else None
        # 变量说明：evidence 表示当前步骤使用的 evidence 值。
        step.evidence = list(evidence)
        # 变量说明：assigned_run_id 表示assigned_run 对象的唯一标识。
        step.assigned_run_id = assigned_run_id or step.assigned_run_id
        # 变量说明：next_action 表示当前步骤使用的 next_action 值。
        step.next_action = ""
        # 变量说明：remaining_work 表示当前步骤使用的 remaining_work 值。
        step.remaining_work = [] if status == "completed" else list(step.remaining_work or [step.title])
        if status == "completed":
            # 变量说明：completed_work 表示当前步骤使用的 completed_work 值。
            step.completed_work = list(step.completed_work or [step.title])
        # 变量说明：completed_at 表示completed_at 对应的时间信息。
        step.completed_at = utcnow()
        refresh_task_state(db, step.task_id)
        db.commit()
        return True
