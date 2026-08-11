"""Read-only usage and cost aggregates for the local dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import UsageRecord, get_db
from app.schemas import UsageModelRead, UsageSummaryRead


router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/summary", response_model=UsageSummaryRead)
def usage_summary(db: Session = Depends(get_db)) -> UsageSummaryRead:
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
    ).one()
    total_requests, input_tokens, output_tokens, cache_creation, cache_read, total_tokens, total_cost = row
    prompt_tokens = int(input_tokens) + int(cache_creation) + int(cache_read)
    cache_hit_rate = (int(cache_read) / prompt_tokens) if prompt_tokens else 0.0
    return UsageSummaryRead(
        total_requests=int(total_requests),
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cache_creation_tokens=int(cache_creation),
        cache_read_tokens=int(cache_read),
        total_tokens=int(total_tokens),
        total_cost_usd=float(total_cost),
        cache_hit_rate=cache_hit_rate,
    )


@router.get("/models", response_model=list[UsageModelRead])
def usage_by_model(db: Session = Depends(get_db)) -> list[UsageModelRead]:
    requests = func.coalesce(func.sum(UsageRecord.request_count), 0)
    tokens = func.coalesce(func.sum(UsageRecord.total_tokens), 0)
    cost = func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)
    rows = db.execute(
        select(UsageRecord.provider, UsageRecord.model_id, requests, tokens, cost)
        .group_by(UsageRecord.provider, UsageRecord.model_id)
        .order_by(cost.desc(), UsageRecord.provider.asc(), UsageRecord.model_id.asc())
    ).all()
    result: list[UsageModelRead] = []
    for provider, model_id, request_count, token_count, total_cost in rows:
        request_total = int(request_count)
        cost_total = float(total_cost)
        result.append(UsageModelRead(
            provider=provider,
            model_id=model_id,
            requests=request_total,
            tokens=int(token_count),
            total_cost_usd=cost_total,
            avg_cost_usd=(cost_total / request_total) if request_total else 0.0,
        ))
    return result
