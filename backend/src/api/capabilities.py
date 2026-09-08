"""Capability catalog and inert Skill package management endpoints."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 capabilities 子模块。
# 逻辑关系：上层通过 api/capabilities.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.persistence.database import Skill, get_db
from src.api.schemas import (
    SkillImportRequest,
    SkillInstallRead,
    SkillMarketBrowseRead,
    SkillMarketInstallRequest,
    SkillMarketLeaderboardsRead,
    SkillMarketSearchRead,
    SkillMarketSearchRequest,
    SkillMarketBrowseView,
    SkillRead,
    ToolRead,
)
from src.skills.registry import (
    browse_market,
    install_local_skill,
    market_leaderboards,
    market_status,
    preview_github_skill,
    preview_market_skill,
    search_market,
    tool_catalog_payload,
    uninstall_skill,
)


# 变量说明：router 表示当前步骤使用的 router 值。
router = APIRouter(prefix="/api", tags=["capabilities"])


# 函数职责：列出 tools 对应的数据或流程。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/tools", response_model=list[ToolRead])
def list_tools() -> list[dict]:
    """List the built-in capability catalog without claiming all are runnable."""

    return tool_catalog_payload()


# 函数职责：列出 skills 对应的数据或流程。
# 参数关系：db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/skills", response_model=list[SkillRead])
def list_skills(db: Session = Depends(get_db)) -> list[Skill]:
    return list(db.scalars(select(Skill).order_by(Skill.installed_at.desc(), Skill.name.asc())))


# 函数职责：完成 import_local_skill 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/skills/import", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def import_local_skill(payload: SkillImportRequest, db: Session = Depends(get_db)) -> Skill:
    """Copy a user-selected local SKILL.md folder into PGAgent data storage."""

    return install_local_skill(db, payload.source_path)


# 函数职责：删除 skill 对应的数据或流程。
# 参数关系：skill_id 表示skill 对象的唯一标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(skill_id: str, db: Session = Depends(get_db)) -> Response:
    """Uninstall a PGAgent-managed Skill and remove it from Agent/session selections."""

    uninstall_skill(db, skill_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 函数职责：读取 market_status 对应的数据或流程。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/skills/market/status", response_model=SkillMarketSearchRead)
def get_market_status() -> SkillMarketSearchRead:
    # 变量说明：available 表示当前步骤使用的 available 值；message 表示当前消息。
    available, message = market_status()
    return SkillMarketSearchRead(available=available, message=message)


# 函数职责：完成 search_skill_market 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/skills/market/search", response_model=SkillMarketSearchRead)
def search_skill_market(payload: SkillMarketSearchRequest) -> SkillMarketSearchRead:
    # 变量说明：available 表示当前步骤使用的 available 值；message 表示当前消息；items 表示待处理的元素集合。
    available, message, items = search_market(payload.query, payload.limit)
    return SkillMarketSearchRead(available=available, message=message, items=items)


# 函数职责：完成 browse_skill_market 对应的业务处理。
# 参数关系：view 表示当前步骤使用的 view 值；page 表示当前步骤使用的 page 值；per_page 表示当前步骤使用的 per_page 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/skills/market/browse", response_model=SkillMarketBrowseRead)
def browse_skill_market(
    view: SkillMarketBrowseView = "all-time",
    page: int = Query(default=0, ge=0, le=10_000),
    per_page: int = Query(default=12, ge=1, le=50),
) -> SkillMarketBrowseRead:
    """Browse popular marketplace Skills while keeping installation explicit."""

    # 变量说明：available 表示当前步骤使用的 available 值；message 表示当前消息；items 表示待处理的元素集合；has_more 表示表示是否满足 _more 条件的布尔标记；total 表示当前步骤使用的 total 值。
    available, message, items, has_more, total = browse_market(view, page=page, per_page=per_page)
    return SkillMarketBrowseRead(
        available=available,
        message=message,
        items=items,
        view=view,
        page=page,
        has_more=has_more,
        total=total,
    )


# 函数职责：读取 market_leaderboards 对应的数据或流程。
# 参数关系：refresh 表示当前步骤使用的 refresh 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/skills/market/leaderboards", response_model=SkillMarketLeaderboardsRead)
def get_market_leaderboards(
    refresh: bool = Query(default=False),
) -> SkillMarketLeaderboardsRead:
    """Read five cached topic leaderboards; ``refresh=true`` rebuilds them."""

    return SkillMarketLeaderboardsRead(**market_leaderboards(refresh=refresh))


# 函数职责：完成 refresh_market_leaderboards 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/skills/market/leaderboards/refresh", response_model=SkillMarketLeaderboardsRead)
def refresh_market_leaderboards() -> SkillMarketLeaderboardsRead:
    """Explicitly rebuild the cached topic leaderboards."""

    return SkillMarketLeaderboardsRead(**market_leaderboards(refresh=True))


# 函数职责：完成 preview_or_install_market_skill 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/skills/market/install", response_model=SkillInstallRead)
def preview_or_install_market_skill(
    payload: SkillMarketInstallRequest, db: Session = Depends(get_db)
) -> SkillInstallRead:
    """Preview a remote Skill first; only copy it when ``confirm`` is true."""

    if payload.market_id:
        # 变量说明：preview 表示当前步骤使用的 preview 值；installed 表示当前步骤使用的 installed 值。
        preview, installed = preview_market_skill(payload.market_id, confirm=payload.confirm, db=db)
    else:
        assert payload.source_url is not None
        # 变量说明：preview 表示当前步骤使用的 preview 值；installed 表示当前步骤使用的 installed 值。
        preview, installed = preview_github_skill(
            payload.source_url,
            skill_path=payload.skill_path,
            confirm=payload.confirm,
            db=db,
        )
    return SkillInstallRead(
        installed=installed is not None,
        source_url=preview.source_url,
        candidates=list(preview.candidates),
        files=[{"path": path, "size": size} for path, size in preview.files],
        skill=SkillRead.model_validate(installed) if installed is not None else None,
        message=None if installed is not None else "Preview only. Set confirm=true to copy this Skill into PGAgent.",
    )
