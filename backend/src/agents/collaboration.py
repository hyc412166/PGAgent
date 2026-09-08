"""Durable teammate identities, mailboxes, and optional Git worktrees."""
# 文件职责：负责子代理协作与委派中的 collaboration 子模块。
# 逻辑关系：上层通过 agents/collaboration.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from src.persistence import database as database_module
from src.config import settings
from src.persistence.database import (
    Agent,
    CollaborationEvent,
    CollaborationMessage,
    CollaborationTeam,
    DelegatedTask,
    Run,
    TeammateWorker,
)
from src.tools.types import ToolResult


# 变量说明：_TEAM_PROVISION_LOCK 表示当前步骤使用的 _TEAM_PROVISION_LOCK 值。
_TEAM_PROVISION_LOCK = threading.RLock()


# 函数职责：完成 worker_payload 对应的业务处理。
# 参数关系：worker 表示当前步骤使用的 worker 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _worker_payload(worker: TeammateWorker) -> dict[str, Any]:
    return {
        "id": worker.id,
        "team_id": worker.team_id,
        "agent_id": worker.agent_id,
        "name": worker.name,
        "role": worker.role,
        "status": worker.status,
        "current_plan_step_id": worker.current_plan_step_id,
        "last_run_id": worker.last_run_id,
        "workspace_mode": worker.workspace_mode,
        "worktree_path": worker.worktree_path,
        "branch_name": worker.branch_name,
        "created_at": worker.created_at.isoformat() if worker.created_at else None,
        "updated_at": worker.updated_at.isoformat() if worker.updated_at else None,
    }


# 函数职责：完成 message_payload 对应的业务处理。
# 参数关系：message 表示当前消息。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _message_payload(message: CollaborationMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "team_id": message.team_id,
        "from": message.sender_worker_id or "lead",
        "to": message.recipient_worker_id or "lead",
        "type": message.message_type,
        "content": message.content,
        "payload": dict(message.payload or {}),
        "read": message.read_at is not None,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


# 函数职责：创建 worktree 对应的数据或流程。
# 参数关系：workspace_root 表示当前步骤使用的 workspace_root 值；worker_id 表示worker 对象的唯一标识；worktree_root 表示当前步骤使用的 worktree_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _create_worktree(
    workspace_root: str,
    worker_id: str,
    worktree_root: Path | None = None,
) -> tuple[str, str]:
    # 变量说明：root 表示处理范围的根目录。
    root = Path(workspace_root).resolve()
    # 变量说明：repository 表示当前步骤使用的 repository 值。
    repository = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if repository.returncode != 0:
        raise ValueError("workspace_mode=worktree requires a Git repository")
    # 变量说明：repository_root 表示当前步骤使用的 repository_root 值。
    repository_root = Path(repository.stdout.strip()).resolve()
    # 变量说明：target 表示当前步骤使用的 target 值。
    target = ((worktree_root or settings.data_dir / "worktrees") / worker_id).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    # 变量说明：branch 表示当前步骤使用的 branch 值。
    branch = f"pgagent/{worker_id[:12]}"
    # 变量说明：created 表示当前步骤使用的 created 值。
    created = subprocess.run(
        ["git", "-C", str(repository_root), "worktree", "add", "-b", branch, str(target), "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if created.returncode != 0:
        raise RuntimeError(created.stderr.strip() or "git worktree add failed")
    return str(target), branch


# 函数职责：完成 cleanup_session_worktrees 对应的业务处理。
# 参数关系：db 表示当前数据库会话；session_id 表示所属会话标识；workspace_root 表示当前步骤使用的 workspace_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def cleanup_session_worktrees(db: Any, session_id: str, workspace_root: str) -> None:
    """Remove Git worktrees and temporary branches owned by one session."""

    # 变量说明：team_ids 表示team 对象标识集合。
    team_ids = select(CollaborationTeam.id).where(CollaborationTeam.session_id == session_id)
    # 变量说明：workers 表示当前流程使用的 workers 集合。
    workers = list(db.scalars(select(TeammateWorker).where(
        TeammateWorker.team_id.in_(team_ids),
        TeammateWorker.workspace_mode == "worktree",
        TeammateWorker.worktree_path.is_not(None),
        TeammateWorker.branch_name.is_not(None),
    )))
    if not workers:
        return
    # 变量说明：repository 表示当前步骤使用的 repository 值。
    repository = subprocess.run(
        ["git", "-C", workspace_root, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if repository.returncode != 0:
        raise RuntimeError(repository.stderr.strip() or "session workspace is no longer a Git repository")
    # 变量说明：repository_root 表示当前步骤使用的 repository_root 值。
    repository_root = repository.stdout.strip()
    # 变量说明：listed 表示当前步骤使用的 listed 值。
    listed = subprocess.run(
        ["git", "-C", repository_root, "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.strip() or "git worktree list failed")
    # 变量说明：registered 表示当前步骤使用的 registered 值。
    registered = {
        str(Path(line.removeprefix("worktree ")).resolve()).casefold()
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ")
    }
    for worker in workers:
        # 变量说明：worktree_path 表示worktree_path 对应的文件系统位置。
        worktree_path = str(Path(str(worker.worktree_path)).resolve())
        # 变量说明：branch_name 表示当前步骤使用的 branch_name 值。
        branch_name = str(worker.branch_name or "")
        if not branch_name.startswith("pgagent/"):
            raise RuntimeError(f"refusing to delete non-PGAgent branch {branch_name}")
        if worktree_path.casefold() in registered:
            # 变量说明：removed 表示当前步骤使用的 removed 值。
            removed = subprocess.run(
                ["git", "-C", repository_root, "worktree", "remove", "--force", worktree_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            if removed.returncode != 0:
                raise RuntimeError(removed.stderr.strip() or f"failed to remove worktree {worktree_path}")
        # 变量说明：branch_exists 表示当前流程使用的 branch_exists 集合。
        branch_exists = subprocess.run(
            ["git", "-C", repository_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if branch_exists.returncode == 0:
            # 变量说明：deleted 表示当前步骤使用的 deleted 值。
            deleted = subprocess.run(
                ["git", "-C", repository_root, "branch", "-D", "--", branch_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if deleted.returncode != 0:
                raise RuntimeError(deleted.stderr.strip() or f"failed to delete branch {branch_name}")
        elif branch_exists.returncode != 1:
            raise RuntimeError(f"failed to inspect branch {branch_name}")


# 类职责：封装 TeamToolStore 的持久化访问。
class TeamToolStore:
    """Run-scoped provider facade over one durable collaboration team."""

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：run_id 表示当前运行标识；session_id 表示所属会话标识；workspace_root 表示当前步骤使用的 workspace_root 值；worktree_root 表示当前步骤使用的 worktree_root 值；actor_worker_id 表示actor_worker 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(
        self,
        *,
        run_id: str,
        session_id: str | None,
        workspace_root: str,
        worktree_root: Path | None = None,
        actor_worker_id: str | None = None,
    ) -> None:
        # 变量说明：run_id 表示当前运行标识。
        self.run_id = run_id
        # 变量说明：session_id 表示所属会话标识。
        self.session_id = session_id
        # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
        self.workspace_root = workspace_root
        # 变量说明：worktree_root 表示当前步骤使用的 worktree_root 值。
        self.worktree_root = worktree_root
        # 变量说明：actor_worker_id 表示actor_worker 对象的唯一标识。
        self.actor_worker_id = actor_worker_id

    # 函数职责：完成 team 对应的业务处理。
    # 参数关系：db 表示当前数据库会话。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _team(self, db: Any) -> CollaborationTeam:
        # 变量说明：team 表示当前步骤使用的 team 值。
        team = db.scalar(select(CollaborationTeam).where(
            CollaborationTeam.parent_run_id == self.run_id
        ))
        if team is not None:
            return team
        # 变量说明：run 表示当前步骤使用的 run 值。
        run = db.get(Run, self.run_id)
        if run is None:
            raise ValueError("parent run does not exist")
        if run.task_id:
            # 变量说明：team 表示当前步骤使用的 team 值。
            team = db.scalar(
                select(CollaborationTeam)
                .where(
                    CollaborationTeam.task_id == run.task_id,
                    CollaborationTeam.status == "active",
                )
                .order_by(CollaborationTeam.updated_at.desc(), CollaborationTeam.id.desc())
            )
            if team is not None:
                # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
                team.parent_run_id = self.run_id
                # 变量说明：session_id 表示所属会话标识。
                team.session_id = self.session_id
                db.flush()
                return team
        # 变量说明：team 表示当前步骤使用的 team 值。
        team = CollaborationTeam(
            parent_run_id=self.run_id,
            session_id=self.session_id,
            task_id=run.task_id,
            name="PGAgent collaboration team",
            status="active",
        )
        db.add(team)
        db.flush()
        return team

    # 函数职责：解析 worker 对应的数据或流程。
    # 参数关系：db 表示当前数据库会话；team_id 表示team 对象的唯一标识；value 表示当前字段或计算值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _resolve_worker(db: Any, team_id: str, value: str) -> TeammateWorker | None:
        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized = str(value or "").strip()
        # 变量说明：worker 表示当前步骤使用的 worker 值。
        worker = db.get(TeammateWorker, normalized)
        if worker is not None and worker.team_id == team_id:
            return worker
        return db.scalar(select(TeammateWorker).where(
            TeammateWorker.team_id == team_id,
            func.lower(TeammateWorker.name) == normalized.casefold(),
        ))

    # 函数职责：完成 spawn 对应的业务处理。
    # 参数关系：name 表示当前对象名称；role 表示当前步骤使用的 role 值；prompt 表示当前步骤使用的 prompt 值；agent_id 表示智能体标识；workspace_mode 表示当前步骤使用的 workspace_mode 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def spawn(
        self,
        *,
        name: str,
        role: str,
        prompt: str,
        agent_id: str = "",
        workspace_mode: str = "shared",
    ) -> ToolResult:
        if self.actor_worker_id:
            return ToolResult(
                "spawn_teammate", False, "only the lead Agent can spawn teammates", error_code="lead_only"
            )
        # 变量说明：normalized_mode 表示当前步骤使用的 normalized_mode 值。
        normalized_mode = str(workspace_mode or "shared").strip().lower()
        if normalized_mode not in {"shared", "worktree"}:
            return ToolResult("spawn_teammate", False, "workspace_mode must be shared or worktree", error_code="invalid_workspace_mode")
        with _TEAM_PROVISION_LOCK:
            with database_module.SessionLocal() as db:
                if db.get_bind().dialect.name == "sqlite":
                    db.execute(text("BEGIN IMMEDIATE"))
                # 变量说明：team 表示当前步骤使用的 team 值。
                team = self._team(db)
                # 变量说明：child 表示当前步骤使用的 child 值。
                child = db.get(Agent, str(agent_id or "").strip()) if agent_id else db.scalar(
                    select(Agent).where(
                        Agent.enabled.is_(True),
                        Agent.is_default.is_(False),
                        func.lower(Agent.name) == str(name or "").strip().casefold(),
                    )
                )
                if child is None or child.is_default or not child.enabled:
                    db.rollback()
                    return ToolResult(
                        "spawn_teammate",
                        False,
                        "agent_id must reference an enabled user-created child Agent",
                        error_code="delegate_agent_not_found",
                    )
                # 变量说明：existing 表示当前步骤使用的 existing 值。
                existing = db.scalar(select(TeammateWorker).where(
                    TeammateWorker.team_id == team.id,
                    func.lower(TeammateWorker.name) == str(name or "").strip().casefold(),
                ))
                if existing is not None and existing.status != "provisioning":
                    # 变量说明：payload 表示跨层传递的数据载荷。
                    payload = _worker_payload(existing)
                    db.commit()
                    return ToolResult(
                        "spawn_teammate",
                        True,
                        json.dumps(payload, ensure_ascii=False),
                        metadata={"teammate_id": existing.id, "reused": True},
                    )
                # 变量说明：worker 表示当前步骤使用的 worker 值。
                worker = existing or TeammateWorker(
                    team_id=team.id,
                    agent_id=child.id,
                    name=str(name or child.name).strip()[:120],
                    role=str(role or "teammate").strip()[:160],
                    prompt=str(prompt or "").strip()[:20_000],
                    status="provisioning" if normalized_mode == "worktree" else "idle",
                    workspace_mode=normalized_mode,
                )
                if existing is None:
                    db.add(worker)
                    db.flush()
                # 变量说明：worker_id 表示worker 对象的唯一标识。
                worker_id = worker.id
                # 变量说明：task_id 表示任务标识。
                task_id = team.task_id
                if normalized_mode == "shared":
                    # 变量说明：payload 表示跨层传递的数据载荷。
                    payload = _worker_payload(worker)
                    db.add(CollaborationEvent(
                        task_id=task_id,
                        run_id=self.run_id,
                        source_kind="teammate",
                        source_id=worker.id,
                        event_type="teammate_spawned",
                        payload=payload,
                    ))
                    db.commit()
                    return ToolResult(
                        "spawn_teammate",
                        True,
                        json.dumps(payload, ensure_ascii=False),
                        changed=True,
                        metadata={"teammate_id": worker.id},
                    )
                db.commit()

            try:
                # 变量说明：worktree_path 表示worktree_path 对应的文件系统位置；branch_name 表示当前步骤使用的 branch_name 值。
                worktree_path, branch_name = _create_worktree(
                    self.workspace_root, worker_id, self.worktree_root
                )
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                with database_module.SessionLocal() as db:
                    # 变量说明：worker 表示当前步骤使用的 worker 值。
                    worker = db.get(TeammateWorker, worker_id)
                    if worker is not None and worker.status == "provisioning":
                        db.delete(worker)
                    db.commit()
                return ToolResult(
                    "spawn_teammate", False, str(exc), error_code="worktree_create_failed"
                )

            with database_module.SessionLocal() as db:
                if db.get_bind().dialect.name == "sqlite":
                    db.execute(text("BEGIN IMMEDIATE"))
                # 变量说明：worker 表示当前步骤使用的 worker 值。
                worker = db.get(TeammateWorker, worker_id)
                if worker is None:
                    db.rollback()
                    return ToolResult(
                        "spawn_teammate", False, "teammate provisioning record disappeared", error_code="teammate_not_found"
                    )
                # 变量说明：worktree_path 表示worktree_path 对应的文件系统位置。
                worker.worktree_path = worktree_path
                # 变量说明：branch_name 表示当前步骤使用的 branch_name 值。
                worker.branch_name = branch_name
                # 变量说明：status 表示当前对象或运行的状态。
                worker.status = "idle"
                # 变量说明：payload 表示跨层传递的数据载荷。
                payload = _worker_payload(worker)
                db.add(CollaborationEvent(
                    task_id=task_id,
                    run_id=self.run_id,
                    source_kind="teammate",
                    source_id=worker.id,
                    event_type="teammate_spawned",
                    payload=payload,
                ))
                db.commit()
                return ToolResult(
                    "spawn_teammate",
                    True,
                    json.dumps(payload, ensure_ascii=False),
                    changed=True,
                    metadata={"teammate_id": worker.id},
                )

    # 函数职责：完成 list 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def list(self) -> ToolResult:
        with database_module.SessionLocal() as db:
            # 变量说明：team 表示当前步骤使用的 team 值。
            team = self._team(db)
            # 变量说明：workers 表示当前流程使用的 workers 集合。
            workers = list(db.scalars(
                select(TeammateWorker)
                .where(TeammateWorker.team_id == team.id)
                .order_by(TeammateWorker.created_at.asc(), TeammateWorker.id.asc())
            ))
            db.commit()
            return ToolResult(
                "list_teammates",
                True,
                json.dumps([_worker_payload(worker) for worker in workers], ensure_ascii=False),
            )

    # 函数职责：完成 send_message 对应的业务处理。
    # 参数关系：to 表示当前步骤使用的 to 值；content 表示待处理或返回的正文内容；sender 表示当前步骤使用的 sender 值；msg_type 表示当前步骤使用的 msg_type 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def send_message(
        self,
        *,
        to: str,
        content: str,
        sender: str = "lead",
        msg_type: str = "message",
    ) -> ToolResult:
        with database_module.SessionLocal() as db:
            # 变量说明：team 表示当前步骤使用的 team 值。
            team = self._team(db)
            # 变量说明：recipient 表示当前步骤使用的 recipient 值。
            recipient = None if str(to).strip().casefold() == "lead" else self._resolve_worker(db, team.id, to)
            if recipient is None and str(to).strip().casefold() != "lead":
                return ToolResult("send_message", False, "teammate does not exist", error_code="teammate_not_found")
            # 变量说明：sender_worker 表示当前步骤使用的 sender_worker 值。
            sender_worker = (
                db.get(TeammateWorker, self.actor_worker_id)
                if self.actor_worker_id else None if sender == "lead" else self._resolve_worker(db, team.id, sender)
            )
            if sender_worker is not None and sender_worker.team_id != team.id:
                return ToolResult("send_message", False, "sender does not belong to this team", error_code="teammate_not_found")
            if self.actor_worker_id and sender_worker is None:
                return ToolResult("send_message", False, "teammate sender does not exist", error_code="teammate_not_found")
            if not self.actor_worker_id and sender != "lead" and sender_worker is None:
                return ToolResult("send_message", False, "teammate sender does not exist", error_code="teammate_not_found")
            # 变量说明：message 表示当前消息。
            message = CollaborationMessage(
                team_id=team.id,
                sender_worker_id=sender_worker.id if sender_worker is not None else None,
                recipient_worker_id=recipient.id if recipient is not None else None,
                message_type=str(msg_type or "message")[:32],
                content=str(content or "")[:20_000],
            )
            db.add(message)
            db.flush()
            db.add(CollaborationEvent(
                task_id=team.task_id,
                run_id=self.run_id,
                source_kind="teammate_message",
                source_id=message.id,
                event_type="teammate_message_sent",
                payload=_message_payload(message),
            ))
            db.commit()
            return ToolResult(
                "send_message", True, json.dumps(_message_payload(message), ensure_ascii=False), changed=True
            )

    # 函数职责：完成 read_inbox 对应的业务处理。
    # 参数关系：recipient 表示当前步骤使用的 recipient 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def read_inbox(self, *, recipient: str = "lead") -> ToolResult:
        with database_module.SessionLocal() as db:
            # 变量说明：team 表示当前步骤使用的 team 值。
            team = self._team(db)
            if self.actor_worker_id:
                # 变量说明：recipient_worker 表示当前步骤使用的 recipient_worker 值。
                recipient_worker = db.get(TeammateWorker, self.actor_worker_id)
            else:
                # 变量说明：recipient_worker 表示当前步骤使用的 recipient_worker 值。
                recipient_worker = None if recipient == "lead" else self._resolve_worker(db, team.id, recipient)
            if recipient != "lead" and recipient_worker is None:
                return ToolResult("read_inbox", False, "teammate does not exist", error_code="teammate_not_found")
            # 变量说明：query 表示当前步骤使用的 query 值。
            query = select(CollaborationMessage).where(
                CollaborationMessage.team_id == team.id,
                CollaborationMessage.recipient_worker_id == (
                    recipient_worker.id if recipient_worker is not None else None
                ),
                CollaborationMessage.read_at.is_(None),
            ).order_by(CollaborationMessage.created_at.asc(), CollaborationMessage.id.asc())
            # 变量说明：messages 表示发送给模型或客户端的消息序列。
            messages = list(db.scalars(query))
            # 变量说明：now 表示当前时间。
            now = database_module.utcnow()
            for message in messages:
                # 变量说明：read_at 表示read_at 对应的时间信息。
                message.read_at = now
            db.commit()
            return ToolResult(
                "read_inbox",
                True,
                json.dumps([_message_payload(message) for message in messages], ensure_ascii=False),
                changed=bool(messages),
            )

    # 函数职责：完成 broadcast 对应的业务处理。
    # 参数关系：content 表示待处理或返回的正文内容。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def broadcast(self, *, content: str) -> ToolResult:
        with database_module.SessionLocal() as db:
            # 变量说明：team 表示当前步骤使用的 team 值。
            team = self._team(db)
            # 变量说明：workers 表示当前流程使用的 workers 集合。
            workers = list(db.scalars(select(TeammateWorker).where(
                TeammateWorker.team_id == team.id,
                TeammateWorker.status.not_in({"stopped", "failed"}),
            )))
            # 变量说明：sender_worker_id 表示sender_worker 对象的唯一标识。
            sender_worker_id = self.actor_worker_id
            # 变量说明：messages 表示发送给模型或客户端的消息序列。
            messages = [CollaborationMessage(
                team_id=team.id,
                sender_worker_id=sender_worker_id,
                recipient_worker_id=worker.id,
                message_type="broadcast",
                content=str(content or "")[:20_000],
            ) for worker in workers if worker.id != sender_worker_id]
            if sender_worker_id:
                messages.append(CollaborationMessage(
                    team_id=team.id,
                    sender_worker_id=sender_worker_id,
                    recipient_worker_id=None,
                    message_type="broadcast",
                    content=str(content or "")[:20_000],
                ))
            db.add_all(messages)
            db.commit()
            return ToolResult(
                "broadcast",
                True,
                json.dumps({
                    "sent_to": [worker.id for worker in workers if worker.id != sender_worker_id]
                    + (["lead"] if sender_worker_id else [])
                }, ensure_ascii=False),
                changed=bool(messages),
            )

    # 函数职责：完成 shutdown 对应的业务处理。
    # 参数关系：teammate 表示当前步骤使用的 teammate 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def shutdown(self, *, teammate: str) -> ToolResult:
        if self.actor_worker_id:
            return ToolResult(
                "shutdown_request", False, "only the lead Agent can stop teammates", error_code="lead_only"
            )
        with database_module.SessionLocal() as db:
            # 变量说明：team 表示当前步骤使用的 team 值。
            team = self._team(db)
            # 变量说明：worker 表示当前步骤使用的 worker 值。
            worker = self._resolve_worker(db, team.id, teammate)
            if worker is None:
                return ToolResult("shutdown_request", False, "teammate does not exist", error_code="teammate_not_found")
            # 变量说明：status 表示当前对象或运行的状态。
            worker.status = "stopping" if worker.status == "working" else "stopped"
            db.add(CollaborationMessage(
                team_id=team.id,
                sender_worker_id=None,
                recipient_worker_id=worker.id,
                message_type="shutdown_request",
                content="Please stop after the current step.",
            ))
            db.commit()
            return ToolResult(
                "shutdown_request", True, json.dumps(_worker_payload(worker), ensure_ascii=False), changed=True
            )

    # 函数职责：完成 integrate 对应的业务处理。
    # 参数关系：teammate 表示当前步骤使用的 teammate 值；commit_message 表示当前步骤使用的 commit_message 值；paths 表示当前流程使用的 paths 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def integrate(
        self,
        *,
        teammate: str,
        commit_message: str = "",
        paths: list[str] | None = None,
    ) -> ToolResult:
        """Commit one idle worktree and merge its branch into the parent repository."""

        if self.actor_worker_id:
            return ToolResult(
                "integrate_teammate", False, "only the lead Agent can integrate worktrees", error_code="lead_only"
            )
        with database_module.SessionLocal() as db:
            # 变量说明：team 表示当前步骤使用的 team 值。
            team = self._team(db)
            # 变量说明：worker 表示当前步骤使用的 worker 值。
            worker = self._resolve_worker(db, team.id, teammate)
            if worker is None:
                return ToolResult(
                    "integrate_teammate", False, "teammate does not exist", error_code="teammate_not_found"
                )
            if worker.workspace_mode != "worktree" or not worker.worktree_path or not worker.branch_name:
                return ToolResult(
                    "integrate_teammate",
                    False,
                    "teammate does not own an isolated worktree",
                    error_code="teammate_has_no_worktree",
                )
            if worker.status in {"working", "stopping"}:
                return ToolResult(
                    "integrate_teammate",
                    False,
                    "teammate is still writing to the worktree",
                    error_code="teammate_busy",
                )
            # 变量说明：worker_id 表示worker 对象的唯一标识。
            worker_id = worker.id
            # 变量说明：worktree_path 表示worktree_path 对应的文件系统位置。
            worktree_path = worker.worktree_path
            # 变量说明：branch_name 表示当前步骤使用的 branch_name 值。
            branch_name = worker.branch_name
            # 变量说明：task_id 表示任务标识。
            task_id = team.task_id

        # 变量说明：repository 表示当前步骤使用的 repository 值。
        repository = subprocess.run(
            ["git", "-C", self.workspace_root, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if repository.returncode != 0:
            return ToolResult(
                "integrate_teammate", False, "parent workspace is not a Git repository", error_code="git_repository_missing"
            )
        # 变量说明：repository_root 表示当前步骤使用的 repository_root 值。
        repository_root = repository.stdout.strip()
        # 变量说明：selected_paths 表示当前流程使用的 selected_paths 集合。
        selected_paths: list[str] = []
        for raw_path in paths or []:
            # 变量说明：normalized 表示当前步骤使用的 normalized 值。
            normalized = str(raw_path or "").strip().replace("\\", "/")
            # 变量说明：candidate 表示当前步骤使用的 candidate 值。
            candidate = Path(normalized)
            if (
                not normalized
                or normalized in {".", "./"}
                or candidate.is_absolute()
                or ".." in candidate.parts
            ):
                return ToolResult(
                    "integrate_teammate",
                    False,
                    f"invalid integration path: {raw_path}",
                    error_code="integration_path_invalid",
                )
            if normalized not in selected_paths:
                selected_paths.append(normalized)
        # 变量说明：worktree_status 表示当前流程使用的 worktree_status 集合。
        worktree_status = subprocess.run(
            ["git", "-C", worktree_path, "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if worktree_status.returncode != 0:
            return ToolResult(
                "integrate_teammate", False, worktree_status.stderr.strip(), error_code="worktree_status_failed"
            )
        if worktree_status.stdout.strip():
            if not selected_paths:
                # 变量说明：changed_paths 表示当前流程使用的 changed_paths 集合。
                changed_paths = [
                    line[3:] for line in worktree_status.stdout.splitlines() if len(line) > 3
                ]
                return ToolResult(
                    "integrate_teammate",
                    False,
                    "worktree has uncommitted changes; pass explicit paths to integrate: "
                    + ", ".join(changed_paths[:50]),
                    error_code="integration_paths_required",
                )
            # 变量说明：unstaged 表示当前步骤使用的 unstaged 值。
            unstaged = subprocess.run(
                ["git", "-C", worktree_path, "reset", "--quiet", "HEAD", "--", "."],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if unstaged.returncode != 0:
                return ToolResult(
                    "integrate_teammate",
                    False,
                    unstaged.stderr.strip(),
                    error_code="worktree_index_reset_failed",
                )
            # 变量说明：added 表示当前步骤使用的 added 值。
            added = subprocess.run(
                ["git", "-C", worktree_path, "add", "--", *selected_paths],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if added.returncode != 0:
                return ToolResult(
                    "integrate_teammate", False, added.stderr.strip(), error_code="worktree_stage_failed"
                )
            # 变量说明：staged 表示当前步骤使用的 staged 值。
            staged = subprocess.run(
                ["git", "-C", worktree_path, "diff", "--cached", "--quiet"],
                capture_output=True,
                timeout=15,
                check=False,
            )
            if staged.returncode not in {0, 1}:
                return ToolResult(
                    "integrate_teammate", False, "failed to inspect staged worktree changes", error_code="worktree_stage_failed"
                )
            if staged.returncode == 1:
                # 变量说明：committed 表示当前步骤使用的 committed 值。
                committed = subprocess.run(
                    [
                        "git", "-C", worktree_path,
                        "-c", "user.name=PGAgent",
                        "-c", "user.email=pgagent@local",
                        "commit", "-m", str(commit_message or f"PGAgent teammate {worker_id[:12]}")[:200],
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=False,
                )
                if committed.returncode != 0:
                    return ToolResult(
                        "integrate_teammate", False, committed.stderr.strip(), error_code="worktree_commit_failed"
                    )

        # 变量说明：pending_commits 表示当前流程使用的 pending_commits 集合。
        pending_commits = subprocess.run(
            ["git", "-C", repository_root, "rev-list", "--count", f"HEAD..{branch_name}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if pending_commits.returncode != 0:
            return ToolResult(
                "integrate_teammate", False, pending_commits.stderr.strip(), error_code="worktree_compare_failed"
            )
        if int(pending_commits.stdout.strip() or "0") == 0:
            return ToolResult(
                "integrate_teammate",
                True,
                json.dumps({"teammate_id": worker_id, "branch": branch_name, "status": "already_integrated"}),
            )

        try:
            # 变量说明：merged 表示当前步骤使用的 merged 值。
            merged = subprocess.run(
                ["git", "-C", repository_root, "merge", "--no-ff", "--no-edit", branch_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["git", "-C", repository_root, "merge", "--abort"],
                capture_output=True,
                timeout=30,
                check=False,
            )
            return ToolResult(
                "integrate_teammate",
                False,
                "git merge timed out and was aborted",
                error_code="worktree_merge_timeout",
            )
        if merged.returncode != 0:
            subprocess.run(
                ["git", "-C", repository_root, "merge", "--abort"],
                capture_output=True,
                timeout=30,
                check=False,
            )
            return ToolResult(
                "integrate_teammate",
                False,
                merged.stderr.strip() or merged.stdout.strip(),
                error_code="worktree_merge_conflict",
            )
        # 变量说明：head 表示当前步骤使用的 head 值。
        head = subprocess.run(
            ["git", "-C", repository_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        ).stdout.strip()
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = {
            "teammate_id": worker_id,
            "branch": branch_name,
            "status": "integrated",
            "merge_commit": head,
        }
        with database_module.SessionLocal() as db:
            db.add(CollaborationEvent(
                task_id=task_id,
                run_id=self.run_id,
                source_kind="teammate",
                source_id=worker_id,
                event_type="teammate_worktree_integrated",
                payload=payload,
            ))
            db.commit()
        return ToolResult(
            "integrate_teammate", True, json.dumps(payload, ensure_ascii=False), changed=True
        )


# 函数职责：完成 teammate_context 对应的业务处理。
# 参数关系：db 表示当前数据库会话；worker 表示当前步骤使用的 worker 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def teammate_context(db: Any, worker: TeammateWorker) -> str:
    """Consume a teammate's inbox and summarize its prior assignments."""

    # 变量说明：messages 表示发送给模型或客户端的消息序列。
    messages = list(db.scalars(
        select(CollaborationMessage)
        .where(
            CollaborationMessage.team_id == worker.team_id,
            CollaborationMessage.recipient_worker_id == worker.id,
            CollaborationMessage.read_at.is_(None),
        )
        .order_by(CollaborationMessage.created_at.asc(), CollaborationMessage.id.asc())
    ))
    # 变量说明：history 表示当前步骤使用的 history 值。
    history = list(db.scalars(
        select(DelegatedTask)
        .where(
            DelegatedTask.teammate_id == worker.id,
            DelegatedTask.status.in_({"completed", "blocked"}),
        )
        .order_by(DelegatedTask.updated_at.desc(), DelegatedTask.id.desc())
        .limit(8)
    ))
    # 变量说明：now 表示当前时间。
    now = database_module.utcnow()
    for message in messages:
        # 变量说明：read_at 表示read_at 对应的时间信息。
        message.read_at = now
    # 变量说明：packet 表示当前步骤使用的 packet 值。
    packet = {
        "teammate": {
            "id": worker.id,
            "name": worker.name,
            "role": worker.role,
            "persistent_prompt": worker.prompt,
        },
        "inbox": [_message_payload(message) for message in messages],
        "prior_assignments": [
            {
                "title": item.title,
                "status": item.status,
                "result": dict(item.result or {}),
            }
            for item in reversed(history)
        ],
    }
    return "<persistent-teammate-context>\n" + json.dumps(packet, ensure_ascii=False)[:20_000] + "\n</persistent-teammate-context>"
