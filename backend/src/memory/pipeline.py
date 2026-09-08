"""Durable asynchronous Phase-1 extraction and Phase-2 consolidation workers."""
# 文件职责：负责长期记忆提取、合并、检索与偏好中的 pipeline 子模块。
# 逻辑关系：上层通过 memory/pipeline.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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


# 函数职责：完成 utcnow 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 函数职责：完成 response_content 对应的业务处理。
# 参数关系：response 表示下游返回的响应。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _response_content(response: Mapping[str, Any]) -> str:
    # 变量说明：choices 表示当前流程使用的 choices 集合。
    choices = response.get("choices")
    # 变量说明：first 表示当前步骤使用的 first 值。
    first = choices[0] if isinstance(choices, list) and choices else {}
    # 变量说明：message 表示当前消息。
    message = first.get("message") if isinstance(first, Mapping) else {}
    return str(message.get("content") or "") if isinstance(message, Mapping) else ""


# 函数职责：完成 provider_config 对应的业务处理。
# 参数关系：job 表示当前步骤使用的 job 值；connection 表示当前步骤使用的 connection 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _provider_config(job: MemoryJob, connection: ModelConnection) -> ProviderConfig:
    # 变量说明：binding 表示当前步骤使用的 binding 值。
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


# 函数职责：完成 legacy_source_end_sequence 对应的业务处理。
# 参数关系：db 表示当前数据库会话；job 表示当前步骤使用的 job 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _legacy_source_end_sequence(db: Any, job: MemoryJob) -> int:
    """Assign old payload-only jobs a stable per-session negative source position."""

    # 变量说明：job_ids 表示job 对象标识集合。
    job_ids = list(db.scalars(select(MemoryJob.id).where(
        MemoryJob.session_id == job.session_id,
        MemoryJob.kind == "extract",
    ).order_by(MemoryJob.created_at.asc(), MemoryJob.id.asc())))
    try:
        return -(job_ids.index(job.id) + 1)
    except ValueError:
        return -1


# 函数职责：完成 set_usage 对应的业务处理。
# 参数关系：job 表示当前步骤使用的 job 值；usage 表示当前步骤使用的 usage 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _set_usage(job: MemoryJob, usage: Mapping[str, Any]) -> None:
    for key in (
        "request_count", "input_tokens", "output_tokens", "cache_creation_tokens",
        "cache_read_tokens", "total_tokens", "cost_usd",
    ):
        setattr(job, key, usage[key])


# 类职责：定义 MemoryPipeline 在本领域中的数据与行为。
class MemoryPipeline:
    """Own memory-model work while RunCoordinator only schedules durable job ids."""

    # 函数职责：异步完成 process 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def process(self, job_id: str) -> None:
        # 变量说明：claim_attempt 表示当前步骤使用的 claim_attempt 值；provider 表示模型供应商。
        claim_attempt, provider = self._claim(job_id)
        if claim_attempt is None or provider is None:
            return
        try:
            with database_module.SessionLocal() as db:
                # 变量说明：job 表示当前步骤使用的 job 值。
                job = db.get(MemoryJob, job_id)
                if job is None:
                    return
                # 变量说明：kind 表示当前步骤使用的 kind 值。
                kind = str(job.kind or "extract")
            if kind == "extract":
                await self._extract(job_id, claim_attempt, provider)
            elif kind == "consolidate":
                await self._consolidate(job_id, claim_attempt, provider)
            else:
                raise ValueError(f"unsupported memory job kind: {kind}")
        except Exception as exc:
            self._retry_or_fail(job_id, claim_attempt, exc)

    # 函数职责：完成 claim 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _claim(self, job_id: str) -> tuple[int | None, ProviderConfig | None]:
        with database_module.SessionLocal() as db:
            # 变量说明：lease_seconds 表示当前流程使用的 lease_seconds 集合。
            lease_seconds = max(300, int(settings.model_timeout_seconds) + 60)
            # 变量说明：claim 表示当前步骤使用的 claim 值。
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
            # 变量说明：job 表示当前步骤使用的 job 值。
            job = db.get(MemoryJob, job_id)
            if job is None:
                return None, None
            # 变量说明：binding 表示当前步骤使用的 binding 值。
            binding = dict((job.payload or {}).get("runtime_binding") or {})
            # 变量说明：connection 表示当前步骤使用的 connection 值。
            connection = db.get(ModelConnection, str(binding.get("model_connection_id") or ""))
            if connection is None or not connection.enabled:
                # 变量说明：status 表示当前对象或运行的状态。
                job.status = "failed"
                # 变量说明：error 表示当前捕获或准备上报的错误。
                job.error = "model connection unavailable"
                # 变量说明：lease_expires_at 表示lease_expires_at 对应的时间信息。
                job.lease_expires_at = None
                if job.kind == "consolidate":
                    self._release_consolidation_reservations(db, job.id)
                db.commit()
                return None, None
            return int(job.attempts), _provider_config(job, connection)

    # 函数职责：异步完成 call 对应的业务处理。
    # 参数关系：provider 表示模型供应商；messages 表示发送给模型或客户端的消息序列；mode 表示当前步骤使用的 mode 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def _call(self, provider: ProviderConfig, messages: list[dict[str, str]], *, mode: str) -> Mapping[str, Any]:
        # 变量说明：response 表示下游返回的响应。
        response = await asyncio.wait_for(
            build_model_call(provider)(messages=messages, tools=[], mode=mode),
            timeout=float(settings.model_timeout_seconds),
        )
        return dict(response) if isinstance(response, Mapping) else {}

    # 函数职责：异步完成 extract 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识；attempt 表示当前步骤使用的 attempt 值；provider 表示模型供应商。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def _extract(self, job_id: str, attempt: int, provider: ProviderConfig) -> None:
        with database_module.SessionLocal() as db:
            # 变量说明：job 表示当前步骤使用的 job 值。
            job = db.get(MemoryJob, job_id)
            if job is None or not job.session_id:
                raise ValueError("extraction job has no source session")
            # 变量说明：session 表示当前步骤使用的 session 值。
            session = db.get(Session, job.session_id)
            # 变量说明：workspace 表示当前步骤使用的 workspace 值。
            workspace = db.get(Workspace, job.workspace_id) if job.workspace_id else None
            # 变量说明：payload 表示跨层传递的数据载荷。
            payload = dict(job.payload or {})
            # 变量说明：end_sequence 表示当前步骤使用的 end_sequence 值。
            end_sequence = int(payload.get("source_end_sequence") or 0)
            # 变量说明：source_turn_id 表示source_turn 对象的唯一标识。
            source_turn_id = str(payload.get("turn_id") or "").strip()
            # 变量说明：has_legacy_payload 表示表示是否满足 _legacy_payload 条件的布尔标记。
            has_legacy_payload = any(
                key in payload for key in ("user_request", "assistant_response", "tool_observations")
            )
            if end_sequence <= 0 and not source_turn_id and has_legacy_payload:
                # 变量说明：end_sequence 表示当前步骤使用的 end_sequence 值。
                end_sequence = _legacy_source_end_sequence(db, job)
            elif end_sequence <= 0 and source_turn_id:
                # 变量说明：end_sequence 表示当前步骤使用的 end_sequence 值。
                end_sequence = int(db.scalar(select(func.max(ChatMessage.sequence)).where(
                    ChatMessage.session_id == job.session_id,
                    ChatMessage.turn_id == source_turn_id,
                )) or 0)
            if end_sequence == 0:
                # 变量说明：end_sequence 表示当前步骤使用的 end_sequence 值。
                end_sequence = int(db.scalar(select(func.max(ChatMessage.sequence)).where(
                    ChatMessage.session_id == job.session_id,
                    ChatMessage.created_at <= job.created_at,
                )) or 0)
            # 变量说明：existing 表示当前步骤使用的 existing 值。
            existing = db.scalar(select(MemoryRollout.id).where(
                MemoryRollout.source_session_id == job.session_id,
                MemoryRollout.source_end_sequence == end_sequence,
            ))
            if existing:
                self._complete(db, job, attempt, {"rollout_id": existing, "duplicate": True})
                db.commit()
                return
            if has_legacy_payload and end_sequence < 0:
                # 变量说明：filtered 表示当前步骤使用的 filtered 值。
                filtered = legacy_rollout_messages(payload)
            else:
                # 变量说明：rows 表示当前流程使用的 rows 集合。
                rows = list(db.scalars(select(ChatMessage).where(
                    ChatMessage.session_id == job.session_id,
                    ChatMessage.sequence <= end_sequence,
                ).order_by(ChatMessage.sequence.asc(), ChatMessage.created_at.asc(), ChatMessage.id.asc())))
                # 变量说明：filtered 表示当前步骤使用的 filtered 值。
                filtered = bounded_rollout_messages(rows)
                if not filtered:
                    # 变量说明：filtered 表示当前步骤使用的 filtered 值。
                    filtered = legacy_rollout_messages(payload)
            # 变量说明：cwd 表示当前步骤使用的 cwd 值。
            cwd = str(workspace.root_path if workspace is not None else payload.get("cwd") or "")

        # 变量说明：response 表示下游返回的响应。
        response = await self._call(provider, extraction_prompt(messages=filtered, cwd=cwd), mode="memory_extract")
        # 变量说明：extracted 表示当前步骤使用的 extracted 值。
        extracted = parse_extraction(_response_content(response))
        if extracted is None:
            raise ValueError("extraction model returned malformed or incomplete JSON")
        # 变量说明：worth_remembering 表示当前步骤使用的 worth_remembering 值。
        worth_remembering = bool(extracted.get("worth_remembering"))
        # 变量说明：usage 表示当前步骤使用的 usage 值。
        usage = normalize_usage(response.get("usage") if isinstance(response.get("usage"), Mapping) else {})
        # 变量说明：consolidation_job_id 表示consolidation_job 对象的唯一标识。
        consolidation_job_id: str | None = None
        with database_module.SessionLocal() as db:
            # 变量说明：job 表示当前步骤使用的 job 值。
            job = self._fence(db, job_id, attempt)
            if job is None:
                return
            # 变量说明：result 表示本步骤产生的结果。
            result: dict[str, Any] = {"worth_remembering": worth_remembering}
            if worth_remembering:
                # 变量说明：extracted 表示当前步骤使用的 extracted 值。
                extracted = {key: value for key, value in extracted.items() if key != "worth_remembering"}
                # 变量说明：rollout 表示当前步骤使用的 rollout 值。
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
                    # 变量说明：rollout 表示当前步骤使用的 rollout 值。
                    rollout = db.scalar(select(MemoryRollout).where(
                        MemoryRollout.source_session_id == job.session_id,
                        MemoryRollout.source_end_sequence == end_sequence,
                    ))
                if rollout is not None:
                    # 变量说明：result 的索引项 表示该语句创建或更新的目标数据。
                    result["rollout_id"] = rollout.id
                    # 变量说明：consolidation 表示当前步骤使用的 consolidation 值。
                    consolidation = db.scalar(select(MemoryJob).where(
                        MemoryJob.run_id == job.run_id,
                        MemoryJob.kind == "consolidate",
                    ))
                    if consolidation is None:
                        # 变量说明：consolidation 表示当前步骤使用的 consolidation 值。
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
                    # 变量说明：consolidation_job_id 表示consolidation_job 对象的唯一标识。
                    consolidation_job_id = consolidation.id
            self._complete(db, job, attempt, result, usage)
            db.commit()
        refresh_memory_markdown_projection()

    # 函数职责：异步完成 consolidate 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识；attempt 表示当前步骤使用的 attempt 值；provider 表示模型供应商。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def _consolidate(self, job_id: str, attempt: int, provider: ProviderConfig) -> None:
        with database_module.SessionLocal() as db:
            # 变量说明：job 表示当前步骤使用的 job 值。
            job = db.get(MemoryJob, job_id)
            if job is None:
                return
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            # 变量说明：rollouts 表示当前流程使用的 rollouts 集合。
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
                # 变量说明：status 表示当前对象或运行的状态。
                rollout.status = "consolidating"
                # 变量说明：consolidation_job_id 表示consolidation_job 对象的唯一标识。
                rollout.consolidation_job_id = job.id
            db.commit()
            if not rollouts:
                # 变量说明：job 表示当前步骤使用的 job 值。
                job = db.get(MemoryJob, job_id)
                if job is None:
                    return
                self._complete(db, job, attempt, {"selected_rollout_ids": [], "no_op": True})
                db.commit()
                return
            # 变量说明：memories 表示当前流程使用的 memories 集合。
            memories = list(db.scalars(select(Memory).where(
                Memory.status == "active",
                or_(Memory.scope == "global", (Memory.scope == "workspace") & (Memory.scope_id == job.workspace_id)),
            ).order_by(Memory.scope.asc(), Memory.name.asc(), Memory.id.asc())))
            # 变量说明：rollout_inputs 表示当前流程使用的 rollout_inputs 集合。
            rollout_inputs = [rollout_payload(item) for item in rollouts]
            # 变量说明：memory_inputs 表示当前流程使用的 memory_inputs 集合。
            memory_inputs = [memory_payload(item) for item in memories]
            # 变量说明：skill_inputs 表示当前流程使用的 skill_inputs 集合。
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

        # 变量说明：response 表示下游返回的响应。
        response = await self._call(
            provider,
            consolidation_prompt(
                rollout_outputs=rollout_inputs,
                existing_memories=memory_inputs,
                existing_skills=skill_inputs,
            ),
            mode="memory_consolidate",
        )
        # 变量说明：operations 表示当前流程使用的 operations 集合。
        operations = parse_consolidation(_response_content(response))
        if operations is None:
            raise ValueError("consolidation model returned an invalid operation set")
        # 变量说明：usage 表示当前步骤使用的 usage 值。
        usage = normalize_usage(response.get("usage") if isinstance(response.get("usage"), Mapping) else {})
        with database_module.SessionLocal() as db:
            # 变量说明：job 表示当前步骤使用的 job 值。
            job = self._fence(db, job_id, attempt)
            if job is None:
                return
            # 变量说明：available_rollouts 表示当前流程使用的 available_rollouts 集合。
            available_rollouts = {
                item.id: item for item in db.scalars(select(MemoryRollout).where(
                    MemoryRollout.workspace_id == job.workspace_id,
                    or_(
                        MemoryRollout.status == "active",
                        MemoryRollout.consolidation_job_id == job.id,
                    ),
                ))
            }
            # 变量说明：stored_ids 表示stored 对象标识集合。
            stored_ids: list[str] = []
            # 变量说明：now 表示当前时间。
            now = _utcnow()
            for operation in operations["upserts"]:
                # 变量说明：sources 表示当前流程使用的 sources 集合。
                sources = [value for value in operation["source_rollout_ids"] if value in available_rollouts]
                if not sources:
                    continue
                # 变量说明：scope 表示当前步骤使用的 scope 值。
                scope = operation["scope"]
                if scope == "global" and operation["memory_type"] not in {"user", "feedback"}:
                    continue
                # 变量说明：item 表示当前步骤使用的 item 值。
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
                # 变量说明：consolidated_at 表示consolidated_at 对应的时间信息。
                item.consolidated_at = now
                # 变量说明：current_extra 表示当前步骤使用的 current_extra 值。
                current_extra = dict(item.extra or {})
                current_extra["source_rollout_ids"] = list(dict.fromkeys([
                    *[str(value) for value in current_extra.get("source_rollout_ids", [])],
                    *sources,
                ]))
                # 变量说明：extra 表示当前步骤使用的 extra 值。
                item.extra = current_extra
                stored_ids.append(item.id)
                # 变量说明：old_id 表示old 对象的唯一标识。
                old_id = operation.get("memory_id")
                if old_id and old_id != item.id:
                    # 变量说明：old 表示当前步骤使用的 old 值。
                    old = db.get(Memory, old_id)
                    if old is not None and old.status == "active" and (
                        old.scope == "global"
                        or (old.scope == "workspace" and old.scope_id == job.workspace_id)
                    ):
                        # 变量说明：status 表示当前对象或运行的状态。
                        old.status = "superseded"
                        # 变量说明：superseded_by 表示当前步骤使用的 superseded_by 值。
                        old.superseded_by = item.id
            for memory_id in operations["archive_memory_ids"]:
                # 变量说明：memory 表示当前步骤使用的 memory 值。
                memory = db.get(Memory, memory_id)
                if memory is not None and memory.status == "active" and (
                    memory.scope == "global" or memory.scope_id == job.workspace_id
                ):
                    # 变量说明：status 表示当前对象或运行的状态。
                    memory.status = "archived"
            # 变量说明：stored_skill_ids 表示stored_skill 对象标识集合。
            stored_skill_ids: list[str] = []
            for operation in operations["skill_upserts"]:
                # 变量说明：sources 表示当前流程使用的 sources 集合。
                sources = [value for value in operation["source_rollout_ids"] if value in available_rollouts]
                if len(sources) < 2:
                    continue
                # 变量说明：skill 表示当前步骤使用的 skill 值。
                skill = db.get(MemorySkill, operation["skill_id"]) if operation["skill_id"] else None
                if skill is not None and skill.workspace_id != job.workspace_id:
                    # 变量说明：skill 表示当前步骤使用的 skill 值。
                    skill = None
                if skill is None:
                    # 变量说明：skill 表示当前步骤使用的 skill 值。
                    skill = db.scalar(select(MemorySkill).where(
                        MemorySkill.workspace_id == job.workspace_id,
                        MemorySkill.name == operation["name"],
                        MemorySkill.status == "active",
                    ))
                if skill is None:
                    # 变量说明：skill 表示当前步骤使用的 skill 值。
                    skill = MemorySkill(workspace_id=job.workspace_id, name=operation["name"], content=operation["content"])
                    db.add(skill)
                # 变量说明：description 表示当前步骤使用的 description 值。
                skill.description = operation["description"]
                # 变量说明：content 表示待处理或返回的正文内容。
                skill.content = operation["content"]
                # 变量说明：keywords 表示当前流程使用的 keywords 集合。
                skill.keywords = operation["keywords"]
                # 变量说明：source_rollout_ids 表示source_rollout 对象标识集合。
                skill.source_rollout_ids = sources
                # 变量说明：status 表示当前对象或运行的状态。
                skill.status = "active"
                db.flush()
                stored_skill_ids.append(skill.id)
            for skill_id in operations["archive_skill_ids"]:
                # 变量说明：skill 表示当前步骤使用的 skill 值。
                skill = db.get(MemorySkill, skill_id)
                if skill is not None and skill.workspace_id == job.workspace_id:
                    # 变量说明：status 表示当前对象或运行的状态。
                    skill.status = "archived"
            # 变量说明：selected 表示当前步骤使用的 selected 值。
            selected = [value for value in operations["selected_rollout_ids"] if value in available_rollouts]
            for rollout_id in selected:
                # 变量说明：selected_for_phase2_at 表示selected_for_phase2_at 对应的时间信息。
                available_rollouts[rollout_id].selected_for_phase2_at = now
                # 变量说明：status 表示当前对象或运行的状态。
                available_rollouts[rollout_id].status = "active"
                # 变量说明：consolidation_job_id 表示consolidation_job 对象的唯一标识。
                available_rollouts[rollout_id].consolidation_job_id = None
            for rollout_id, rollout in available_rollouts.items():
                if rollout.consolidation_job_id == job.id and rollout_id not in selected:
                    # 变量说明：status 表示当前对象或运行的状态。
                    rollout.status = "active"
                    # 变量说明：consolidation_job_id 表示consolidation_job 对象的唯一标识。
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

    # 函数职责：完成 fence 对应的业务处理。
    # 参数关系：db 表示当前数据库会话；job_id 表示job 对象的唯一标识；attempt 表示当前步骤使用的 attempt 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _fence(db: Any, job_id: str, attempt: int) -> MemoryJob | None:
        # 变量说明：fence 表示当前步骤使用的 fence 值。
        fence = db.execute(update(MemoryJob).where(
            MemoryJob.id == job_id,
            MemoryJob.status == "running",
            MemoryJob.attempts == attempt,
        ).values(status="committing"))
        if fence.rowcount != 1:
            db.rollback()
            return None
        return db.get(MemoryJob, job_id)

    # 函数职责：完成 complete 对应的业务处理。
    # 参数关系：db 表示当前数据库会话；job 表示当前步骤使用的 job 值；attempt 表示当前步骤使用的 attempt 值；result 表示本步骤产生的结果；usage 表示当前步骤使用的 usage 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
        # 变量说明：status 表示当前对象或运行的状态。
        job.status = "completed"
        # 变量说明：lease_expires_at 表示lease_expires_at 对应的时间信息。
        job.lease_expires_at = None
        # 变量说明：result 表示本步骤产生的结果。
        job.result = dict(result)
        if usage is not None:
            _set_usage(job, usage)

    # 函数职责：完成 release_consolidation_reservations 对应的业务处理。
    # 参数关系：db 表示当前数据库会话；job_id 表示job 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _release_consolidation_reservations(db: Any, job_id: str) -> None:
        db.execute(update(MemoryRollout).where(
            MemoryRollout.consolidation_job_id == job_id,
            MemoryRollout.selected_for_phase2_at.is_(None),
        ).values(status="active", consolidation_job_id=None))

    # 函数职责：完成 retry_or_fail 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识；attempt 表示当前步骤使用的 attempt 值；exc 表示当前捕获的异常。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _retry_or_fail(job_id: str, attempt: int, exc: Exception) -> None:
        with database_module.SessionLocal() as db:
            # 变量说明：terminal 表示当前步骤使用的 terminal 值。
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


# 变量说明：memory_pipeline 表示当前步骤使用的 memory_pipeline 值。
memory_pipeline = MemoryPipeline()


# 函数职责：完成 activate_deferred_memory_jobs 对应的业务处理。
# 参数关系：session_id 表示所属会话标识；exclude_run_id 表示exclude_run 对象的唯一标识；idle_seconds 表示当前流程使用的 idle_seconds 集合；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def activate_deferred_memory_jobs(
    *,
    session_id: str | None = None,
    exclude_run_id: str | None = None,
    idle_seconds: int = 60,
    limit: int = 20,
) -> list[str]:
    """Make completed historical root rollouts eligible without blocking their reply."""

    # 变量说明：cutoff 表示当前步骤使用的 cutoff 值。
    cutoff = _utcnow() - timedelta(seconds=max(0, int(idle_seconds)))
    with database_module.SessionLocal() as db:
        if not memories_enabled(db):
            return []
        # 变量说明：query 表示当前步骤使用的 query 值。
        query = select(MemoryJob).where(
            MemoryJob.kind == "extract",
            MemoryJob.status == "deferred",
            MemoryJob.created_at <= cutoff,
        )
        if session_id:
            # 变量说明：query 表示当前步骤使用的 query 值。
            query = query.where(MemoryJob.session_id == session_id)
        if exclude_run_id:
            # 变量说明：query 表示当前步骤使用的 query 值。
            query = query.where(MemoryJob.run_id != exclude_run_id)
        # 变量说明：candidates 表示当前流程使用的 candidates 集合。
        candidates = list(db.scalars(query.order_by(
            MemoryJob.session_id.asc(), MemoryJob.created_at.desc(), MemoryJob.id.desc()
        ).limit(max(limit * 10, limit))))
        # 变量说明：jobs 表示当前流程使用的 jobs 集合。
        jobs: list[MemoryJob] = []
        # 变量说明：latest_by_session 表示当前步骤使用的 latest_by_session 值。
        latest_by_session: dict[str, MemoryJob] = {}
        for job in candidates:
            # 变量说明：key 表示用于查找或映射的键。
            key = str(job.session_id or job.id)
            # 变量说明：winner 表示当前步骤使用的 winner 值。
            winner = latest_by_session.get(key)
            if winner is None and len(jobs) < limit:
                latest_by_session[key] = job
                jobs.append(job)
                continue
            if winner is not None:
                # 变量说明：status 表示当前对象或运行的状态。
                job.status = "completed"
                # 变量说明：result 表示本步骤产生的结果。
                job.result = {"superseded_by_job_id": winner.id, "worth_remembering": False}
        for job in jobs:
            # 变量说明：status 表示当前对象或运行的状态。
            job.status = "pending"
        db.commit()
        return [job.id for job in jobs]
