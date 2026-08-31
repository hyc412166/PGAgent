"""Durable asynchronous Phase-1 extraction and Phase-2 consolidation workers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from src.agent import normalize_usage
from src.config import settings
from src.memory.consolidation import consolidation_prompt, parse_consolidation
from src.memory.extraction import (
    bounded_rollout_messages,
    extraction_prompt,
    legacy_rollout_messages,
    parse_extraction,
    redact_secrets,
)
from src.memory.repository import rollout_payload
from src.memory.preferences import memories_enabled
from src.memory.service import memory_payload, refresh_memory_markdown_projection, store_memory
from src.model.gateway import ProviderConfig, build_model_call
from src.persistence import database as database_module
from src.persistence.database import (
    ChatMessage,
    Memory,
    MemoryJob,
    MemoryRollout,
    MemorySkill,
    ModelConnection,
    Session,
    Workspace,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _response_content(response: Mapping[str, Any]) -> str:
    choices = response.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, Mapping) else {}
    return str(message.get("content") or "") if isinstance(message, Mapping) else ""


def _provider_config(job: MemoryJob, connection: ModelConnection) -> ProviderConfig:
    binding = dict((job.payload or {}).get("runtime_binding") or {})
    return ProviderConfig(
        provider=str(binding.get("provider") or connection.provider),
        base_url=str(binding.get("base_url") or connection.base_url),
        secret_ref=str(binding.get("secret_ref") or connection.secret_ref),
        model_id=str(binding.get("model_id") or connection.default_model or ""),
        model_connection_id=connection.id,
        thinking_level="off",
        custom_headers=dict(connection.custom_headers or {}),
        api_protocol=str(binding.get("api_protocol") or connection.api_protocol or "chat_completions"),
    )


def _legacy_source_end_sequence(db: Any, job: MemoryJob) -> int:
    """Assign old payload-only jobs a stable per-session negative source position."""

    job_ids = list(db.scalars(select(MemoryJob.id).where(
        MemoryJob.session_id == job.session_id,
        MemoryJob.kind == "extract",
    ).order_by(MemoryJob.created_at.asc(), MemoryJob.id.asc())))
    try:
        return -(job_ids.index(job.id) + 1)
    except ValueError:
        return -1


def _set_usage(job: MemoryJob, usage: Mapping[str, Any]) -> None:
    for key in (
        "request_count", "input_tokens", "output_tokens", "cache_creation_tokens",
        "cache_read_tokens", "total_tokens", "cost_usd",
    ):
        setattr(job, key, usage[key])


class MemoryPipeline:
    """Own memory-model work while RunCoordinator only schedules durable job ids."""

    async def process(self, job_id: str) -> None:
        claim_attempt, provider = self._claim(job_id)
        if claim_attempt is None or provider is None:
            return
        try:
            with database_module.SessionLocal() as db:
                job = db.get(MemoryJob, job_id)
                if job is None:
                    return
                kind = str(job.kind or "extract")
            if kind == "extract":
                await self._extract(job_id, claim_attempt, provider)
            elif kind == "consolidate":
                await self._consolidate(job_id, claim_attempt, provider)
            else:
                raise ValueError(f"unsupported memory job kind: {kind}")
        except Exception as exc:
            self._retry_or_fail(job_id, claim_attempt, exc)

    def _claim(self, job_id: str) -> tuple[int | None, ProviderConfig | None]:
        with database_module.SessionLocal() as db:
            lease_seconds = max(300, int(settings.model_timeout_seconds) + 60)
            claim = db.execute(
                update(MemoryJob)
                .where(MemoryJob.id == job_id, MemoryJob.status == "pending", MemoryJob.attempts < 3)
                .values(
                    status="running",
                    attempts=MemoryJob.attempts + 1,
                    error=None,
                    lease_expires_at=_utcnow() + timedelta(seconds=lease_seconds),
                )
            )
            if claim.rowcount != 1:
                db.rollback()
                return None, None
            db.commit()
            job = db.get(MemoryJob, job_id)
            if job is None:
                return None, None
            binding = dict((job.payload or {}).get("runtime_binding") or {})
            connection = db.get(ModelConnection, str(binding.get("model_connection_id") or ""))
            if connection is None or not connection.enabled:
                job.status = "failed"
                job.error = "model connection unavailable"
                job.lease_expires_at = None
                if job.kind == "consolidate":
                    self._release_consolidation_reservations(db, job.id)
                db.commit()
                return None, None
            return int(job.attempts), _provider_config(job, connection)

    async def _call(self, provider: ProviderConfig, messages: list[dict[str, str]], *, mode: str) -> Mapping[str, Any]:
        response = await asyncio.wait_for(
            build_model_call(provider)(messages=messages, tools=[], mode=mode),
            timeout=float(settings.model_timeout_seconds),
        )
        return dict(response) if isinstance(response, Mapping) else {}

    async def _extract(self, job_id: str, attempt: int, provider: ProviderConfig) -> None:
        with database_module.SessionLocal() as db:
            job = db.get(MemoryJob, job_id)
            if job is None or not job.session_id:
                raise ValueError("extraction job has no source session")
            session = db.get(Session, job.session_id)
            workspace = db.get(Workspace, job.workspace_id) if job.workspace_id else None
            payload = dict(job.payload or {})
            end_sequence = int(payload.get("source_end_sequence") or 0)
            source_turn_id = str(payload.get("turn_id") or "").strip()
            has_legacy_payload = any(
                key in payload for key in ("user_request", "assistant_response", "tool_observations")
            )
            if end_sequence <= 0 and not source_turn_id and has_legacy_payload:
                end_sequence = _legacy_source_end_sequence(db, job)
            elif end_sequence <= 0 and source_turn_id:
                end_sequence = int(db.scalar(select(func.max(ChatMessage.sequence)).where(
                    ChatMessage.session_id == job.session_id,
                    ChatMessage.turn_id == source_turn_id,
                )) or 0)
            if end_sequence == 0:
                end_sequence = int(db.scalar(select(func.max(ChatMessage.sequence)).where(
                    ChatMessage.session_id == job.session_id,
                    ChatMessage.created_at <= job.created_at,
                )) or 0)
            existing = db.scalar(select(MemoryRollout.id).where(
                MemoryRollout.source_session_id == job.session_id,
                MemoryRollout.source_end_sequence == end_sequence,
            ))
            if existing:
                self._complete(db, job, attempt, {"rollout_id": existing, "duplicate": True})
                db.commit()
                return
            if has_legacy_payload and end_sequence < 0:
                filtered = legacy_rollout_messages(payload)
            else:
                rows = list(db.scalars(select(ChatMessage).where(
                    ChatMessage.session_id == job.session_id,
                    ChatMessage.sequence <= end_sequence,
                ).order_by(ChatMessage.sequence.asc(), ChatMessage.created_at.asc(), ChatMessage.id.asc())))
                filtered = bounded_rollout_messages(rows)
                if not filtered:
                    filtered = legacy_rollout_messages(payload)
            cwd = str(workspace.root_path if workspace is not None else payload.get("cwd") or "")

        response = await self._call(provider, extraction_prompt(messages=filtered, cwd=cwd), mode="memory_extract")
        extracted = parse_extraction(_response_content(response))
        if extracted is None:
            raise ValueError("extraction model returned malformed or incomplete JSON")
        worth_remembering = bool(extracted.get("worth_remembering"))
        usage = normalize_usage(response.get("usage") if isinstance(response.get("usage"), Mapping) else {})
        consolidation_job_id: str | None = None
        with database_module.SessionLocal() as db:
            job = self._fence(db, job_id, attempt)
            if job is None:
                return
            result: dict[str, Any] = {"worth_remembering": worth_remembering}
            if worth_remembering:
                extracted = {key: value for key, value in extracted.items() if key != "worth_remembering"}
                rollout = MemoryRollout(
                    source_session_id=str(job.session_id),
                    source_turn_id=str((job.payload or {}).get("turn_id") or "") or None,
                    workspace_id=job.workspace_id,
                    extraction_job_id=job.id,
                    source_end_sequence=end_sequence,
                    cwd=cwd,
                    **extracted,
                )
                try:
                    with db.begin_nested():
                        db.add(rollout)
                        db.flush()
                except IntegrityError:
                    rollout = db.scalar(select(MemoryRollout).where(
                        MemoryRollout.source_session_id == job.session_id,
                        MemoryRollout.source_end_sequence == end_sequence,
                    ))
                if rollout is not None:
                    result["rollout_id"] = rollout.id
                    consolidation = db.scalar(select(MemoryJob).where(
                        MemoryJob.run_id == job.run_id,
                        MemoryJob.kind == "consolidate",
                    ))
                    if consolidation is None:
                        consolidation = MemoryJob(
                            run_id=job.run_id,
                            session_id=job.session_id,
                            workspace_id=job.workspace_id,
                            kind="consolidate",
                            status="pending",
                            payload={
                                "trigger_rollout_id": rollout.id,
                                "runtime_binding": dict((job.payload or {}).get("runtime_binding") or {}),
                            },
                        )
                        db.add(consolidation)
                        db.flush()
                    consolidation_job_id = consolidation.id
            self._complete(db, job, attempt, result, usage)
            db.commit()
        refresh_memory_markdown_projection()

    async def _consolidate(self, job_id: str, attempt: int, provider: ProviderConfig) -> None:
        with database_module.SessionLocal() as db:
            job = db.get(MemoryJob, job_id)
            if job is None:
                return
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            rollouts = list(db.scalars(select(MemoryRollout).where(
                MemoryRollout.workspace_id == job.workspace_id,
                MemoryRollout.selected_for_phase2_at.is_(None),
                or_(
                    MemoryRollout.consolidation_job_id == job.id,
                    (MemoryRollout.status == "active") & MemoryRollout.consolidation_job_id.is_(None),
                ),
            ).order_by(
                (MemoryRollout.consolidation_job_id == job.id).desc(),
                MemoryRollout.created_at.asc(),
                MemoryRollout.id.asc(),
            ).limit(50)))
            for rollout in rollouts:
                rollout.status = "consolidating"
                rollout.consolidation_job_id = job.id
            db.commit()
            if not rollouts:
                job = db.get(MemoryJob, job_id)
                if job is None:
                    return
                self._complete(db, job, attempt, {"selected_rollout_ids": [], "no_op": True})
                db.commit()
                return
            memories = list(db.scalars(select(Memory).where(
                Memory.status == "active",
                or_(Memory.scope == "global", (Memory.scope == "workspace") & (Memory.scope_id == job.workspace_id)),
            ).order_by(Memory.scope.asc(), Memory.name.asc(), Memory.id.asc())))
            rollout_inputs = [rollout_payload(item) for item in rollouts]
            memory_inputs = [memory_payload(item) for item in memories]
            skill_inputs = [{
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "content": item.content,
                "keywords": list(item.keywords or []),
                "source_rollout_ids": list(item.source_rollout_ids or []),
                "usage_count": int(item.usage_count or 0),
            } for item in db.scalars(select(MemorySkill).where(
                MemorySkill.workspace_id == job.workspace_id,
                MemorySkill.status == "active",
            ))]

        response = await self._call(
            provider,
            consolidation_prompt(
                rollout_outputs=rollout_inputs,
                existing_memories=memory_inputs,
                existing_skills=skill_inputs,
            ),
            mode="memory_consolidate",
        )
        operations = parse_consolidation(_response_content(response))
        if operations is None:
            raise ValueError("consolidation model returned an invalid operation set")
        usage = normalize_usage(response.get("usage") if isinstance(response.get("usage"), Mapping) else {})
        with database_module.SessionLocal() as db:
            job = self._fence(db, job_id, attempt)
            if job is None:
                return
            available_rollouts = {
                item.id: item for item in db.scalars(select(MemoryRollout).where(
                    MemoryRollout.workspace_id == job.workspace_id,
                    or_(
                        MemoryRollout.status == "active",
                        MemoryRollout.consolidation_job_id == job.id,
                    ),
                ))
            }
            stored_ids: list[str] = []
            now = _utcnow()
            for operation in operations["upserts"]:
                sources = [value for value in operation["source_rollout_ids"] if value in available_rollouts]
                if not sources:
                    continue
                scope = operation["scope"]
                if scope == "global" and operation["memory_type"] not in {"user", "feedback"}:
                    continue
                item = store_memory(
                    db,
                    name=operation["name"],
                    content=operation["content"],
                    memory_type=operation["memory_type"],
                    description=operation["description"],
                    tags=operation["tags"],
                    scope=scope,
                    workspace_id=job.workspace_id,
                    session_id=job.session_id,
                    metadata={"source": "phase2_consolidation", "source_rollout_ids": sources},
                )
                item.consolidated_at = now
                current_extra = dict(item.extra or {})
                current_extra["source_rollout_ids"] = list(dict.fromkeys([
                    *[str(value) for value in current_extra.get("source_rollout_ids", [])],
                    *sources,
                ]))
                item.extra = current_extra
                stored_ids.append(item.id)
                old_id = operation.get("memory_id")
                if old_id and old_id != item.id:
                    old = db.get(Memory, old_id)
                    if old is not None and old.status == "active" and (
                        old.scope == "global"
                        or (old.scope == "workspace" and old.scope_id == job.workspace_id)
                    ):
                        old.status = "superseded"
                        old.superseded_by = item.id
            for memory_id in operations["archive_memory_ids"]:
                memory = db.get(Memory, memory_id)
                if memory is not None and memory.status == "active" and (
                    memory.scope == "global" or memory.scope_id == job.workspace_id
                ):
                    memory.status = "archived"
            stored_skill_ids: list[str] = []
            for operation in operations["skill_upserts"]:
                sources = [value for value in operation["source_rollout_ids"] if value in available_rollouts]
                if len(sources) < 2:
                    continue
                skill = db.get(MemorySkill, operation["skill_id"]) if operation["skill_id"] else None
                if skill is not None and skill.workspace_id != job.workspace_id:
                    skill = None
                if skill is None:
                    skill = db.scalar(select(MemorySkill).where(
                        MemorySkill.workspace_id == job.workspace_id,
                        MemorySkill.name == operation["name"],
                        MemorySkill.status == "active",
                    ))
                if skill is None:
                    skill = MemorySkill(workspace_id=job.workspace_id, name=operation["name"], content=operation["content"])
                    db.add(skill)
                skill.description = operation["description"]
                skill.content = operation["content"]
                skill.keywords = operation["keywords"]
                skill.source_rollout_ids = sources
                skill.status = "active"
                db.flush()
                stored_skill_ids.append(skill.id)
            for skill_id in operations["archive_skill_ids"]:
                skill = db.get(MemorySkill, skill_id)
                if skill is not None and skill.workspace_id == job.workspace_id:
                    skill.status = "archived"
            selected = [value for value in operations["selected_rollout_ids"] if value in available_rollouts]
            for rollout_id in selected:
                available_rollouts[rollout_id].selected_for_phase2_at = now
                available_rollouts[rollout_id].status = "active"
                available_rollouts[rollout_id].consolidation_job_id = None
            for rollout_id, rollout in available_rollouts.items():
                if rollout.consolidation_job_id == job.id and rollout_id not in selected:
                    rollout.status = "active"
                    rollout.consolidation_job_id = None
            self._complete(
                db,
                job,
                attempt,
                {"memory_ids": stored_ids, "skill_ids": stored_skill_ids, "selected_rollout_ids": selected},
                usage,
            )
            db.commit()
        refresh_memory_markdown_projection()

    @staticmethod
    def _fence(db: Any, job_id: str, attempt: int) -> MemoryJob | None:
        fence = db.execute(update(MemoryJob).where(
            MemoryJob.id == job_id,
            MemoryJob.status == "running",
            MemoryJob.attempts == attempt,
        ).values(status="committing"))
        if fence.rowcount != 1:
            db.rollback()
            return None
        return db.get(MemoryJob, job_id)

    @staticmethod
    def _complete(
        db: Any,
        job: MemoryJob,
        attempt: int,
        result: Mapping[str, Any],
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        if job.attempts != attempt:
            return
        job.status = "completed"
        job.lease_expires_at = None
        job.result = dict(result)
        if usage is not None:
            _set_usage(job, usage)

    @staticmethod
    def _release_consolidation_reservations(db: Any, job_id: str) -> None:
        db.execute(update(MemoryRollout).where(
            MemoryRollout.consolidation_job_id == job_id,
            MemoryRollout.selected_for_phase2_at.is_(None),
        ).values(status="active", consolidation_job_id=None))

    @staticmethod
    def _retry_or_fail(job_id: str, attempt: int, exc: Exception) -> None:
        with database_module.SessionLocal() as db:
            terminal = attempt >= 3
            db.execute(update(MemoryJob).where(
                MemoryJob.id == job_id,
                MemoryJob.status.in_({"running", "committing"}),
                MemoryJob.attempts == attempt,
            ).values(
                status="failed" if terminal else "pending",
                error=redact_secrets(f"{type(exc).__name__}: {str(exc)[:1000]}"),
                lease_expires_at=None,
            ))
            if terminal:
                MemoryPipeline._release_consolidation_reservations(db, job_id)
            db.commit()


memory_pipeline = MemoryPipeline()


def activate_deferred_memory_jobs(
    *,
    session_id: str | None = None,
    exclude_run_id: str | None = None,
    idle_seconds: int = 60,
    limit: int = 20,
) -> list[str]:
    """Make completed historical root rollouts eligible without blocking their reply."""

    cutoff = _utcnow() - timedelta(seconds=max(0, int(idle_seconds)))
    with database_module.SessionLocal() as db:
        if not memories_enabled(db):
            return []
        query = select(MemoryJob).where(
            MemoryJob.kind == "extract",
            MemoryJob.status == "deferred",
            MemoryJob.created_at <= cutoff,
        )
        if session_id:
            query = query.where(MemoryJob.session_id == session_id)
        if exclude_run_id:
            query = query.where(MemoryJob.run_id != exclude_run_id)
        candidates = list(db.scalars(query.order_by(
            MemoryJob.session_id.asc(), MemoryJob.created_at.desc(), MemoryJob.id.desc()
        ).limit(max(limit * 10, limit))))
        jobs: list[MemoryJob] = []
        latest_by_session: dict[str, MemoryJob] = {}
        for job in candidates:
            key = str(job.session_id or job.id)
            winner = latest_by_session.get(key)
            if winner is None and len(jobs) < limit:
                latest_by_session[key] = job
                jobs.append(job)
                continue
            if winner is not None:
                job.status = "completed"
                job.result = {"superseded_by_job_id": winner.id, "worth_remembering": False}
        for job in jobs:
            job.status = "pending"
        db.commit()
        return [job.id for job in jobs]
