"""Read-only usage and cost aggregates for the local dashboard."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 usage 子模块。
# 逻辑关系：上层通过 api/usage.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.persistence.database import Run, Session as ChatSession, UsageRecord, Workspace, get_db
from src.api.schemas import UsageModelRead, UsageRunRead, UsageSessionRead, UsageSummaryRead, UsageWorkspaceRead


# 变量说明：router 表示当前步骤使用的 router 值。
router = APIRouter(prefix="/api/usage", tags=["usage"])


# 函数职责：完成 usage_range_filters 对应的业务处理。
# 参数关系：start_at 表示start_at 对应的时间信息；end_at 表示end_at 对应的时间信息。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _usage_range_filters(start_at: datetime | None, end_at: datetime | None) -> list[object]:
    """Build inclusive filters against the time a usage record was created."""

    # 变量说明：filters 表示当前流程使用的 filters 集合。
    filters: list[object] = []
    if start_at is not None:
        filters.append(UsageRecord.created_at >= start_at)
    if end_at is not None:
        filters.append(UsageRecord.created_at <= end_at)
    return filters


# 函数职责：完成 cache_hit_rate 对应的业务处理。
# 参数关系：input_tokens 表示当前流程使用的 input_tokens 集合；cache_creation_tokens 表示当前流程使用的 cache_creation_tokens 集合；cache_read_tokens 表示当前流程使用的 cache_read_tokens 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _cache_hit_rate(
    input_tokens: int, cache_creation_tokens: int, cache_read_tokens: int
) -> float:
    # 变量说明：prompt_tokens 表示当前流程使用的 prompt_tokens 集合。
    prompt_tokens = input_tokens + cache_creation_tokens + cache_read_tokens
    return (cache_read_tokens / prompt_tokens) if prompt_tokens else 0.0


# 函数职责：完成 user_facing_usage_filters 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _user_facing_usage_filters() -> list[object]:
    """Hide empty, unlinked placeholder rows without losing historical usage."""

    # 变量说明：has_usage 表示表示是否满足 _usage 条件的布尔标记。
    has_usage = (UsageRecord.total_tokens > 0) | (UsageRecord.cost_usd > 0)
    # 变量说明：has_link 表示表示是否满足 _link 条件的布尔标记。
    has_link = UsageRecord.run_id.is_not(None) | UsageRecord.session_id.is_not(None)
    return [has_usage | has_link]


# 函数职责：完成 usage_summary 对应的业务处理。
# 参数关系：start_at 表示start_at 对应的时间信息；end_at 表示end_at 对应的时间信息；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/summary", response_model=UsageSummaryRead)
def usage_summary(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> UsageSummaryRead:
    # 变量说明：row 表示当前步骤使用的 row 值。
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
    # 变量说明：total_requests 表示当前流程使用的 total_requests 集合；input_tokens 表示当前流程使用的 input_tokens 集合；output_tokens 表示当前流程使用的 output_tokens 集合；cache_creation 表示当前步骤使用的 cache_creation 值；cache_read 表示当前步骤使用的 cache_read 值；其余名称为同组解包值。
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


# 函数职责：完成 usage_by_run 对应的业务处理。
# 参数关系：run_id 表示当前运行标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/runs/{run_id}", response_model=UsageRunRead | None)
def usage_by_run(run_id: str, db: Session = Depends(get_db)) -> UsageRunRead | None:
    """Return the exact usage record for one run, when the provider reported it."""

    # 变量说明：record 表示当前步骤使用的 record 值。
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


# 函数职责：完成 usage_by_model 对应的业务处理。
# 参数关系：start_at 表示start_at 对应的时间信息；end_at 表示end_at 对应的时间信息；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/models", response_model=list[UsageModelRead])
def usage_by_model(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> list[UsageModelRead]:
    # 变量说明：requests 表示当前流程使用的 requests 集合。
    requests = func.coalesce(func.sum(UsageRecord.request_count), 0)
    # 变量说明：tokens 表示当前流程使用的 tokens 集合。
    tokens = func.coalesce(func.sum(UsageRecord.total_tokens), 0)
    # 变量说明：cost 表示当前步骤使用的 cost 值。
    cost = func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)
    # 变量说明：input_tokens 表示当前流程使用的 input_tokens 集合。
    input_tokens = func.coalesce(func.sum(UsageRecord.input_tokens), 0)
    # 变量说明：cache_creation 表示当前步骤使用的 cache_creation 值。
    cache_creation = func.coalesce(func.sum(UsageRecord.cache_creation_tokens), 0)
    # 变量说明：cache_read 表示当前步骤使用的 cache_read 值。
    cache_read = func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0)
    # 变量说明：rows 表示当前流程使用的 rows 集合。
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
    # 变量说明：result 表示本步骤产生的结果。
    result: list[UsageModelRead] = []
    for provider, model_id, request_count, token_count, total_cost, input_total, cache_created, cache_hits in rows:
        # 变量说明：request_total 表示当前步骤使用的 request_total 值。
        request_total = int(request_count)
        # 变量说明：cost_total 表示当前步骤使用的 cost_total 值。
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


# 函数职责：完成 usage_by_session 对应的业务处理。
# 参数关系：start_at 表示start_at 对应的时间信息；end_at 表示end_at 对应的时间信息；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/sessions", response_model=list[UsageSessionRead])
def usage_by_session(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> list[UsageSessionRead]:
    # 变量说明：requests 表示当前流程使用的 requests 集合。
    requests = func.coalesce(func.sum(UsageRecord.request_count), 0)
    # 变量说明：tokens 表示当前流程使用的 tokens 集合。
    tokens = func.coalesce(func.sum(UsageRecord.total_tokens), 0)
    # 变量说明：cost 表示当前步骤使用的 cost 值。
    cost = func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)
    # 变量说明：input_tokens 表示当前流程使用的 input_tokens 集合。
    input_tokens = func.coalesce(func.sum(UsageRecord.input_tokens), 0)
    # 变量说明：cache_creation 表示当前步骤使用的 cache_creation 值。
    cache_creation = func.coalesce(func.sum(UsageRecord.cache_creation_tokens), 0)
    # 变量说明：cache_read 表示当前步骤使用的 cache_read 值。
    cache_read = func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0)
    # 变量说明：session_id 表示所属会话标识。
    session_id = func.coalesce(UsageRecord.session_id, Run.session_id)
    # 变量说明：rows 表示当前流程使用的 rows 集合。
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
    # 变量说明：result 表示本步骤产生的结果。
    result: list[UsageSessionRead] = []
    for session_id, title, request_count, token_count, total_cost, input_total, cache_created, cache_hits in rows:
        # 变量说明：request_total 表示当前步骤使用的 request_total 值。
        request_total = int(request_count)
        # 变量说明：cost_total 表示当前步骤使用的 cost_total 值。
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


# 函数职责：完成 usage_by_workspace 对应的业务处理。
# 参数关系：start_at 表示start_at 对应的时间信息；end_at 表示end_at 对应的时间信息；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/workspaces", response_model=list[UsageWorkspaceRead])
def usage_by_workspace(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> list[UsageWorkspaceRead]:
    # 变量说明：requests 表示当前流程使用的 requests 集合。
    requests = func.coalesce(func.sum(UsageRecord.request_count), 0)
    # 变量说明：tokens 表示当前流程使用的 tokens 集合。
    tokens = func.coalesce(func.sum(UsageRecord.total_tokens), 0)
    # 变量说明：cost 表示当前步骤使用的 cost 值。
    cost = func.coalesce(func.sum(UsageRecord.cost_usd), 0.0)
    # 变量说明：input_tokens 表示当前流程使用的 input_tokens 集合。
    input_tokens = func.coalesce(func.sum(UsageRecord.input_tokens), 0)
    # 变量说明：cache_creation 表示当前步骤使用的 cache_creation 值。
    cache_creation = func.coalesce(func.sum(UsageRecord.cache_creation_tokens), 0)
    # 变量说明：cache_read 表示当前步骤使用的 cache_read 值。
    cache_read = func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0)
    # 变量说明：session_id 表示所属会话标识。
    session_id = func.coalesce(UsageRecord.session_id, Run.session_id)
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id = func.coalesce(Run.workspace_id, ChatSession.workspace_id)
    # 变量说明：rows 表示当前流程使用的 rows 集合。
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
    # 变量说明：result 表示本步骤产生的结果。
    result: list[UsageWorkspaceRead] = []
    for item_id, name, path, request_count, token_count, total_cost, input_total, cache_created, cache_hits in rows:
        # 变量说明：request_total 表示当前步骤使用的 request_total 值。
        request_total = int(request_count)
        # 变量说明：cost_total 表示当前步骤使用的 cost_total 值。
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
