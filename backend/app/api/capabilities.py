"""Capability catalog and inert Skill package management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Skill, get_db
from app.schemas import (
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
from app.services.skill_service import (
    browse_market,
    install_local_skill,
    market_leaderboards,
    market_status,
    preview_github_skill,
    preview_market_skill,
    search_market,
    tool_catalog_payload,
)


router = APIRouter(prefix="/api", tags=["capabilities"])


@router.get("/tools", response_model=list[ToolRead])
def list_tools() -> list[dict]:
    """List the built-in capability catalog without claiming all are runnable."""

    return tool_catalog_payload()


@router.get("/skills", response_model=list[SkillRead])
def list_skills(db: Session = Depends(get_db)) -> list[Skill]:
    return list(db.scalars(select(Skill).order_by(Skill.installed_at.desc(), Skill.name.asc())))


@router.post("/skills/import", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def import_local_skill(payload: SkillImportRequest, db: Session = Depends(get_db)) -> Skill:
    """Copy a user-selected local SKILL.md folder into PGAgent data storage."""

    return install_local_skill(db, payload.source_path)


@router.get("/skills/market/status", response_model=SkillMarketSearchRead)
def get_market_status() -> SkillMarketSearchRead:
    available, message = market_status()
    return SkillMarketSearchRead(available=available, message=message)


@router.post("/skills/market/search", response_model=SkillMarketSearchRead)
def search_skill_market(payload: SkillMarketSearchRequest) -> SkillMarketSearchRead:
    available, message, items = search_market(payload.query, payload.limit)
    return SkillMarketSearchRead(available=available, message=message, items=items)


@router.get("/skills/market/browse", response_model=SkillMarketBrowseRead)
def browse_skill_market(
    view: SkillMarketBrowseView = "all-time",
    page: int = Query(default=0, ge=0, le=10_000),
    per_page: int = Query(default=12, ge=1, le=50),
) -> SkillMarketBrowseRead:
    """Browse popular marketplace Skills while keeping installation explicit."""

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


@router.get("/skills/market/leaderboards", response_model=SkillMarketLeaderboardsRead)
def get_market_leaderboards(
    refresh: bool = Query(default=False),
) -> SkillMarketLeaderboardsRead:
    """Read five cached topic leaderboards; ``refresh=true`` rebuilds them."""

    return SkillMarketLeaderboardsRead(**market_leaderboards(refresh=refresh))


@router.post("/skills/market/leaderboards/refresh", response_model=SkillMarketLeaderboardsRead)
def refresh_market_leaderboards() -> SkillMarketLeaderboardsRead:
    """Explicitly rebuild the cached topic leaderboards."""

    return SkillMarketLeaderboardsRead(**market_leaderboards(refresh=True))


@router.post("/skills/market/install", response_model=SkillInstallRead)
def preview_or_install_market_skill(
    payload: SkillMarketInstallRequest, db: Session = Depends(get_db)
) -> SkillInstallRead:
    """Preview a remote Skill first; only copy it when ``confirm`` is true."""

    if payload.market_id:
        preview, installed = preview_market_skill(payload.market_id, confirm=payload.confirm, db=db)
    else:
        assert payload.source_url is not None
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
