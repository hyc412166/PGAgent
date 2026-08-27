"""Durable teammate identities, mailboxes, and optional Git worktrees."""

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


_TEAM_PROVISION_LOCK = threading.RLock()


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


def _create_worktree(
    workspace_root: str,
    worker_id: str,
    worktree_root: Path | None = None,
) -> tuple[str, str]:
    root = Path(workspace_root).resolve()
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
    repository_root = Path(repository.stdout.strip()).resolve()
    target = ((worktree_root or settings.data_dir / "worktrees") / worker_id).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    branch = f"pgagent/{worker_id[:12]}"
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


def cleanup_session_worktrees(db: Any, session_id: str, workspace_root: str) -> None:
    """Remove Git worktrees and temporary branches owned by one session."""

    team_ids = select(CollaborationTeam.id).where(CollaborationTeam.session_id == session_id)
    workers = list(db.scalars(select(TeammateWorker).where(
        TeammateWorker.team_id.in_(team_ids),
        TeammateWorker.workspace_mode == "worktree",
        TeammateWorker.worktree_path.is_not(None),
        TeammateWorker.branch_name.is_not(None),
    )))
    if not workers:
        return
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
    repository_root = repository.stdout.strip()
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
    registered = {
        str(Path(line.removeprefix("worktree ")).resolve()).casefold()
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ")
    }
    for worker in workers:
        worktree_path = str(Path(str(worker.worktree_path)).resolve())
        branch_name = str(worker.branch_name or "")
        if not branch_name.startswith("pgagent/"):
            raise RuntimeError(f"refusing to delete non-PGAgent branch {branch_name}")
        if worktree_path.casefold() in registered:
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
        branch_exists = subprocess.run(
            ["git", "-C", repository_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if branch_exists.returncode == 0:
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


class TeamToolStore:
    """Run-scoped provider facade over one durable collaboration team."""

    def __init__(
        self,
        *,
        run_id: str,
        session_id: str | None,
        workspace_root: str,
        worktree_root: Path | None = None,
        actor_worker_id: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.workspace_root = workspace_root
        self.worktree_root = worktree_root
        self.actor_worker_id = actor_worker_id

    def _team(self, db: Any) -> CollaborationTeam:
        team = db.scalar(select(CollaborationTeam).where(
            CollaborationTeam.parent_run_id == self.run_id
        ))
        if team is not None:
            return team
        run = db.get(Run, self.run_id)
        if run is None:
            raise ValueError("parent run does not exist")
        if run.task_id:
            team = db.scalar(
                select(CollaborationTeam)
                .where(
                    CollaborationTeam.task_id == run.task_id,
                    CollaborationTeam.status == "active",
                )
                .order_by(CollaborationTeam.updated_at.desc(), CollaborationTeam.id.desc())
            )
            if team is not None:
                team.parent_run_id = self.run_id
                team.session_id = self.session_id
                db.flush()
                return team
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

    @staticmethod
    def _resolve_worker(db: Any, team_id: str, value: str) -> TeammateWorker | None:
        normalized = str(value or "").strip()
        worker = db.get(TeammateWorker, normalized)
        if worker is not None and worker.team_id == team_id:
            return worker
        return db.scalar(select(TeammateWorker).where(
            TeammateWorker.team_id == team_id,
            func.lower(TeammateWorker.name) == normalized.casefold(),
        ))

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
        normalized_mode = str(workspace_mode or "shared").strip().lower()
        if normalized_mode not in {"shared", "worktree"}:
            return ToolResult("spawn_teammate", False, "workspace_mode must be shared or worktree", error_code="invalid_workspace_mode")
        with _TEAM_PROVISION_LOCK:
            with database_module.SessionLocal() as db:
                if db.get_bind().dialect.name == "sqlite":
                    db.execute(text("BEGIN IMMEDIATE"))
                team = self._team(db)
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
                existing = db.scalar(select(TeammateWorker).where(
                    TeammateWorker.team_id == team.id,
                    func.lower(TeammateWorker.name) == str(name or "").strip().casefold(),
                ))
                if existing is not None and existing.status != "provisioning":
                    payload = _worker_payload(existing)
                    db.commit()
                    return ToolResult(
                        "spawn_teammate",
                        True,
                        json.dumps(payload, ensure_ascii=False),
                        metadata={"teammate_id": existing.id, "reused": True},
                    )
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
                worker_id = worker.id
                task_id = team.task_id
                if normalized_mode == "shared":
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
                worktree_path, branch_name = _create_worktree(
                    self.workspace_root, worker_id, self.worktree_root
                )
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                with database_module.SessionLocal() as db:
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
                worker = db.get(TeammateWorker, worker_id)
                if worker is None:
                    db.rollback()
                    return ToolResult(
                        "spawn_teammate", False, "teammate provisioning record disappeared", error_code="teammate_not_found"
                    )
                worker.worktree_path = worktree_path
                worker.branch_name = branch_name
                worker.status = "idle"
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

    def list(self) -> ToolResult:
        with database_module.SessionLocal() as db:
            team = self._team(db)
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

    def send_message(
        self,
        *,
        to: str,
        content: str,
        sender: str = "lead",
        msg_type: str = "message",
    ) -> ToolResult:
        with database_module.SessionLocal() as db:
            team = self._team(db)
            recipient = None if str(to).strip().casefold() == "lead" else self._resolve_worker(db, team.id, to)
            if recipient is None and str(to).strip().casefold() != "lead":
                return ToolResult("send_message", False, "teammate does not exist", error_code="teammate_not_found")
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

    def read_inbox(self, *, recipient: str = "lead") -> ToolResult:
        with database_module.SessionLocal() as db:
            team = self._team(db)
            if self.actor_worker_id:
                recipient_worker = db.get(TeammateWorker, self.actor_worker_id)
            else:
                recipient_worker = None if recipient == "lead" else self._resolve_worker(db, team.id, recipient)
            if recipient != "lead" and recipient_worker is None:
                return ToolResult("read_inbox", False, "teammate does not exist", error_code="teammate_not_found")
            query = select(CollaborationMessage).where(
                CollaborationMessage.team_id == team.id,
                CollaborationMessage.recipient_worker_id == (
                    recipient_worker.id if recipient_worker is not None else None
                ),
                CollaborationMessage.read_at.is_(None),
            ).order_by(CollaborationMessage.created_at.asc(), CollaborationMessage.id.asc())
            messages = list(db.scalars(query))
            now = database_module.utcnow()
            for message in messages:
                message.read_at = now
            db.commit()
            return ToolResult(
                "read_inbox",
                True,
                json.dumps([_message_payload(message) for message in messages], ensure_ascii=False),
                changed=bool(messages),
            )

    def broadcast(self, *, content: str) -> ToolResult:
        with database_module.SessionLocal() as db:
            team = self._team(db)
            workers = list(db.scalars(select(TeammateWorker).where(
                TeammateWorker.team_id == team.id,
                TeammateWorker.status.not_in({"stopped", "failed"}),
            )))
            sender_worker_id = self.actor_worker_id
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

    def shutdown(self, *, teammate: str) -> ToolResult:
        if self.actor_worker_id:
            return ToolResult(
                "shutdown_request", False, "only the lead Agent can stop teammates", error_code="lead_only"
            )
        with database_module.SessionLocal() as db:
            team = self._team(db)
            worker = self._resolve_worker(db, team.id, teammate)
            if worker is None:
                return ToolResult("shutdown_request", False, "teammate does not exist", error_code="teammate_not_found")
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
            team = self._team(db)
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
            worker_id = worker.id
            worktree_path = worker.worktree_path
            branch_name = worker.branch_name
            task_id = team.task_id

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
        repository_root = repository.stdout.strip()
        selected_paths: list[str] = []
        for raw_path in paths or []:
            normalized = str(raw_path or "").strip().replace("\\", "/")
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
        head = subprocess.run(
            ["git", "-C", repository_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        ).stdout.strip()
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


def teammate_context(db: Any, worker: TeammateWorker) -> str:
    """Consume a teammate's inbox and summarize its prior assignments."""

    messages = list(db.scalars(
        select(CollaborationMessage)
        .where(
            CollaborationMessage.team_id == worker.team_id,
            CollaborationMessage.recipient_worker_id == worker.id,
            CollaborationMessage.read_at.is_(None),
        )
        .order_by(CollaborationMessage.created_at.asc(), CollaborationMessage.id.asc())
    ))
    history = list(db.scalars(
        select(DelegatedTask)
        .where(
            DelegatedTask.teammate_id == worker.id,
            DelegatedTask.status.in_({"completed", "blocked"}),
        )
        .order_by(DelegatedTask.updated_at.desc(), DelegatedTask.id.desc())
        .limit(8)
    ))
    now = database_module.utcnow()
    for message in messages:
        message.read_at = now
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
