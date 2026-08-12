"""Read-only usage and cost aggregates for the local dashboard."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import Run, Session as ChatSession, UsageRecord, Workspace, get_db
from app.schemas import UsageModelRead, UsageRunRead, UsageSessionRead, UsageSummaryRead, UsageWorkspaceRead


router = APIRouter(prefix="/api/usage", tags=["usage"])


def _usage_range_filters(start_at: datetime | None, end_at: datetime | None) -> list[object]:
    """Build inclusive filters against the time a usage record was created."""

    filters: list[object] = []
    if start_at is not None:
        filters.append(UsageRecord.created_at >= start_at)
    if end_at is not None:
        filters.append(UsageRecord.created_at <= end_at)
    return filters


def _cache_hit_rate(
    input_tokens: int, cache_creation_tokens: int, cache_read_tokens: int
) -> float:
    prompt_tokens = input_tokens + cache_creation_tokens + cache_read_tokens
    return (cache_read_tokens / prompt_tokens) if prompt_tokens else 0.0


def _user_facing_usage_filters() -> list[object]:
    """Hide empty, unlinked placeholder rows without losing historical usage."""

    has_usage = (UsageRecord.total_tokens > 0) | (UsageRecord.cost_usd > 0)
    has_link = UsageRecord.run_id.is_not(None) | UsageRecord.session_id.is_not(None)
    return [has_usage | has_link]


@router.get("/summary", response_model=UsageSummaryRead)
def usage_summary(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> UsageSummaryRead:
    row = db.execute(
        select(
            func.coalesce(func.sum(UsageRecord.request_count), 0),
            func.coalesce(func.sum(UsageRecord.input_tokens), 0),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0),
            func.coalesce(func.sum(UsageRecord.cache_creation_tokens), 0),
            func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0),
            func.coalesce(func.sum(UsageRecord.total_tokens), 0),
            func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
        )
        .where(*_usage_range_filters(start_at, end_at), *_user_facing_usage_filters())
    ).one()
    total_requests, input_tokens, output_tokens, cache_creation, cache_read, total_tokens, total_cost = row
    return UsageSummaryRead(
        total_requests=int(total_requests),
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cache_creation_tokens=int(cache_creation),
        cache_read_tokens=int(cache_read),
        total_tokens=int(total_tokens),
        total_cost_usd=float(total_cost),
        cache_hit_rate=_cache_hit_rate(int(input_tokens), int(cache_creation), int(cache_read)),
    )


@router.get("/runs/{run_id}", response_model=UsageRunRead | None)
def usage_by_run(run_id: str, db: Session = Depends(get_db)) -> UsageRunRead | None:
    """Return the exact usage record for one run, when the provider reported it."""

    record = db.scalar(select(UsageRecord).where(UsageRecord.run_id == run_id))
    if record is None:
        return None
    return UsageRunRead(
        run_id=run_id,
        provider=record.provider,
        model_id=record.model_id,
        requests=int(record.request_count),
        input_tokens=int(record.input_tokens),
        output_tokens=int(record.output_tokens),
        cache_creation_tokens=int(record.cache_creation_tokens),
        cache_read_tokens=int(record.cache_read_tokens),
        total_tokens=int(record.total_tokens),
        total_cost_usd=float(record.cost_usd),
        cache_hit_rate=_cache_hit_rate(
            int(record.input_tokens),
            int(record.cache_creation_tokens),
            int(record.cache_read_tokens),
        ),
    )


@router.get("/models", response_model=list[UsageModelRead])
def usage_by_model(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> list[UsageModelRead]:
    requests = func.coalesce(func.sum(UsageRecord.request_count), 0)
    tokens = func.coalesce(func.sum(UsageRecord.total_tokens), 0)
    cost = func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)
    input_tokens = func.coalesce(func.sum(UsageRecord.input_tokens), 0)
    cache_creation = func.coalesce(func.sum(UsageRecord.cache_creation_tokens), 0)
    cache_read = func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0)
    rows = db.execute(
        select(
            UsageRecord.provider,
            UsageRecord.model_id,
            requests,
            tokens,
            cost,
            input_tokens,
            cache_creation,
            cache_read,
        )
        .where(*_usage_range_filters(start_at, end_at), *_user_facing_usage_filters())
        .group_by(UsageRecord.provider, UsageRecord.model_id)
        .order_by(cost.desc(), UsageRecord.provider.asc(), UsageRecord.model_id.asc())
    ).all()
    result: list[UsageModelRead] = []
    for provider, model_id, request_count, token_count, total_cost, input_total, cache_created, cache_hits in rows:
        request_total = int(request_count)
        cost_total = float(total_cost)
        result.append(UsageModelRead(
            provider=provider,
            model_id=model_id,
            requests=request_total,
            tokens=int(token_count),
            total_cost_usd=cost_total,
            avg_cost_usd=(cost_total / request_total) if request_total else 0.0,
            cache_hit_rate=_cache_hit_rate(int(input_total), int(cache_created), int(cache_hits)),
        ))
    return result


@router.get("/sessions", response_model=list[UsageSessionRead])
def usage_by_session(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> list[UsageSessionRead]:
    requests = func.coalesce(func.sum(UsageRecord.request_count), 0)
    tokens = func.coalesce(func.sum(UsageRecord.total_tokens), 0)
    cost = func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)
    input_tokens = func.coalesce(func.sum(UsageRecord.input_tokens), 0)
    cache_creation = func.coalesce(func.sum(UsageRecord.cache_creation_tokens), 0)
    cache_read = func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0)
    session_id = func.coalesce(UsageRecord.session_id, Run.session_id)
    rows = db.execute(
        select(
            session_id,
            ChatSession.title,
            requests,
            tokens,
            cost,
            input_tokens,
            cache_creation,
            cache_read,
        )
        .select_from(UsageRecord)
        .outerjoin(Run, UsageRecord.run_id == Run.id)
        .outerjoin(ChatSession, session_id == ChatSession.id)
        .where(*_usage_range_filters(start_at, end_at), *_user_facing_usage_filters(), ChatSession.id.is_not(None))
        .group_by(session_id, ChatSession.title)
        .order_by(cost.desc(), ChatSession.title.asc(), session_id.asc())
    ).all()
    result: list[UsageSessionRead] = []
    for session_id, title, request_count, token_count, total_cost, input_total, cache_created, cache_hits in rows:
        request_total = int(request_count)
        cost_total = float(total_cost)
        result.append(UsageSessionRead(
            session_id=session_id,
            title=title,
            requests=request_total,
            tokens=int(token_count),
            total_cost_usd=cost_total,
            avg_cost_usd=(cost_total / request_total) if request_total else 0.0,
            cache_hit_rate=_cache_hit_rate(int(input_total), int(cache_created), int(cache_hits)),
        ))
    return result


@router.get("/workspaces", response_model=list[UsageWorkspaceRead])
def usage_by_workspace(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> list[UsageWorkspaceRead]:
    requests = func.coalesce(func.sum(UsageRecord.request_count), 0)
    tokens = func.coalesce(func.sum(UsageRecord.total_tokens), 0)
    cost = func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)
    input_tokens = func.coalesce(func.sum(UsageRecord.input_tokens), 0)
    cache_creation = func.coalesce(func.sum(UsageRecord.cache_creation_tokens), 0)
    cache_read = func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0)
    session_id = func.coalesce(UsageRecord.session_id, Run.session_id)
    workspace_id = func.coalesce(Run.workspace_id, ChatSession.workspace_id)
    rows = db.execute(
        select(
            workspace_id,
            Workspace.name,
            Workspace.root_path,
            requests,
            tokens,
            cost,
            input_tokens,
            cache_creation,
            cache_read,
        )
        .select_from(UsageRecord)
        .outerjoin(Run, UsageRecord.run_id == Run.id)
        .outerjoin(ChatSession, session_id == ChatSession.id)
        .outerjoin(Workspace, Workspace.id == workspace_id)
        .where(*_usage_range_filters(start_at, end_at), *_user_facing_usage_filters(), Workspace.id.is_not(None))
        .group_by(workspace_id, Workspace.name, Workspace.root_path)
        .order_by(cost.desc(), Workspace.name.asc(), workspace_id.asc())
    ).all()
    result: list[UsageWorkspaceRead] = []
    for item_id, name, path, request_count, token_count, total_cost, input_total, cache_created, cache_hits in rows:
        request_total = int(request_count)
        cost_total = float(total_cost)
        result.append(UsageWorkspaceRead(
            workspace_id=item_id,
            name=name,
            path=path,
            requests=request_total,
            tokens=int(token_count),
            total_cost_usd=cost_total,
            avg_cost_usd=(cost_total / request_total) if request_total else 0.0,
            cache_hit_rate=_cache_hit_rate(int(input_total), int(cache_created), int(cache_hits)),
        ))
    return result
