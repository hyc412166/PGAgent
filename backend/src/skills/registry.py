"""Safe persistence and transport helpers for PGAgent Skills and capabilities.

This module deliberately never imports or executes a file from an installed
Skill.  A Skill is configuration and copied source material until a later
runtime integration explicitly supports it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.tools.catalog import BUILTIN_TOOL_BY_ID, BUILTIN_TOOL_CATALOG
from src.config import settings
from src.persistence.database import Agent, AgentSkill, AgentTool, Session as ChatSession, SessionSkill, Skill


MAX_SKILL_FILES = 200
MAX_SKILL_FILE_BYTES = 2 * 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_FILES = 5_000
MAX_ARCHIVE_EXPANDED_BYTES = 50 * 1024 * 1024
SKILLS_SH_BASE_URL = "https://skills.sh"
PROJECT_ROOT = settings.data_dir.parent
LOCAL_ENV_FILE = PROJECT_ROOT / ".env.local"
SKILL_TOKEN_REFRESH_SCRIPT = PROJECT_ROOT / "scripts" / "refresh-skills-token.ps1"
MARKET_BROWSE_VIEWS = frozenset({"all-time", "trending", "hot", "curated"})
MARKET_LEADERBOARD_LIMIT = 6
MARKET_LEADERBOARD_SEARCH_LIMIT = 50
MARKET_LEADERBOARD_TTL_SECONDS = 30 * 60
GITHUB_ALLOWED_HOSTS = frozenset({"github.com", "api.github.com", "codeload.github.com"})
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SAFE_GITHUB_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_MARKET_TOKEN_REFRESH_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class MarketLeaderboardDefinition:
    """A user-facing Skills marketplace category backed by semantic search."""

    id: str
    name: str
    description: str
    query: str


MARKET_LEADERBOARD_CATEGORIES: tuple[MarketLeaderboardDefinition, ...] = (
    MarketLeaderboardDefinition(
        id="frontend",
        name="前端开发",
        description="React、Next.js、CSS、组件与 Web UI 开发。",
        query="frontend web React Next.js UI development",
    ),
    MarketLeaderboardDefinition(
        id="programming",
        name="编程开发",
        description="软件工程、调试、测试、后端与开发工作流。",
        query="software engineering programming coding development testing",
    ),
    MarketLeaderboardDefinition(
        id="research",
        name="论文研究",
        description="文献检索、学术研究、论文阅读与引用。",
        query="academic research paper literature review citations",
    ),
    MarketLeaderboardDefinition(
        id="writing",
        name="写作内容",
        description="内容创作、技术写作、文案与编辑。",
        query="writing content copywriting documentation editing",
    ),
    MarketLeaderboardDefinition(
        id="data-ai",
        name="数据分析 / AI",
        description="数据分析、机器学习、AI 与模型开发。",
        query="data analysis machine learning AI data science",
    ),
)


@dataclass(frozen=True, slots=True)
class _MarketLeaderboardCache:
    categories: tuple[dict[str, Any], ...]
    refreshed_at: datetime
    expires_at: datetime
    expires_at_monotonic: float


_market_leaderboard_cache: _MarketLeaderboardCache | None = None
_market_leaderboard_lock = threading.RLock()


@dataclass(frozen=True, slots=True)
class SkillManifest:
    slug: str
    name: str
    description: str
    version: str | None


@dataclass(frozen=True, slots=True)
class SkillPreview:
    source_url: str
    candidates: tuple[str, ...]
    files: tuple[tuple[str, int], ...]
    selected_root: Path | None


def tool_catalog_payload() -> list[dict[str, Any]]:
    """Return the static tool catalog in the public API shape."""

    return [
        {
            "id": item.id,
            "name": item.name,
            "label": item.label,
            "description": item.description,
            "category": item.category,
            "risk_level": item.risk_level,
            "enabled": item.enabled,
            "is_builtin": True,
            "availability": item.availability,
            "runtime_tool_id": item.runtime_tool_id,
            "requires_approval": item.requires_approval,
        }
        for item in BUILTIN_TOOL_CATALOG
    ]


def _normalized_ids(values: Iterable[str] | None, *, label: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        normalized = str(value).strip()
        if not normalized:
            raise HTTPException(status_code=422, detail=f"{label} must not contain blank values")
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def validate_tool_ids(tool_ids: Iterable[str] | None) -> list[str]:
    normalized = _normalized_ids(tool_ids, label="tool_ids")
    unknown = [tool_id for tool_id in normalized if tool_id not in BUILTIN_TOOL_BY_ID]
    if unknown:
        raise HTTPException(status_code=422, detail={"unknown_tool_ids": unknown})
    return normalized


def validate_skill_ids(db: Session, skill_ids: Iterable[str] | None) -> list[str]:
    normalized = _normalized_ids(skill_ids, label="skill_ids")
    if not normalized:
        return []
    rows = list(db.scalars(select(Skill).where(Skill.id.in_(normalized))))
    by_id = {row.id: row for row in rows}
    missing = [skill_id for skill_id in normalized if skill_id not in by_id]
    if missing:
        raise HTTPException(status_code=422, detail={"unknown_skill_ids": missing})
    disabled = [skill_id for skill_id in normalized if not by_id[skill_id].enabled]
    if disabled:
        raise HTTPException(status_code=409, detail={"disabled_skill_ids": disabled})
    return normalized


def replace_agent_capabilities(
    db: Session,
    agent: Agent,
    *,
    tool_ids: Iterable[str] | None = None,
    skill_ids: Iterable[str] | None = None,
    replace_tools: bool = False,
    replace_skills: bool = False,
) -> None:
    """Replace explicit relations only when the matching field was supplied."""

    if replace_tools:
        normalized_tools = validate_tool_ids(tool_ids)
        db.execute(delete(AgentTool).where(AgentTool.agent_id == agent.id))
        db.add_all(AgentTool(agent_id=agent.id, tool_id=tool_id) for tool_id in normalized_tools)
    if replace_skills:
        normalized_skills = validate_skill_ids(db, skill_ids)
        db.execute(delete(AgentSkill).where(AgentSkill.agent_id == agent.id))
        db.add_all(AgentSkill(agent_id=agent.id, skill_id=skill_id) for skill_id in normalized_skills)


def replace_session_skills(
    db: Session,
    chat_session: ChatSession,
    skill_ids: Iterable[str] | None,
) -> None:
    normalized_skills = validate_skill_ids(db, skill_ids)
    db.execute(delete(SessionSkill).where(SessionSkill.session_id == chat_session.id))
    db.add_all(SessionSkill(session_id=chat_session.id, skill_id=skill_id) for skill_id in normalized_skills)


def _skills_root() -> Path:
    return settings.data_dir / "skills"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    slug = slug[:100].strip("-")
    if not slug:
        raise HTTPException(status_code=422, detail="Skill name cannot produce a safe slug")
    return slug


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def parse_skill_manifest(skill_file: Path, *, fallback_name: str) -> SkillManifest:
    """Extract conservative metadata without evaluating YAML or markdown code."""

    try:
        text = skill_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="SKILL.md must be UTF-8 text") from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"Cannot read SKILL.md: {exc}") from exc

    lines = text.splitlines()
    frontmatter: dict[str, str] = {}
    body_start = 0
    if lines and lines[0].strip() == "---":
        closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
        if closing is None:
            raise HTTPException(status_code=422, detail="SKILL.md frontmatter is not closed")
        for line in lines[1:closing]:
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {"name", "description", "version"}:
                frontmatter[key.strip().lower()] = _strip_quotes(value)
        body_start = closing + 1

    heading = next((line[2:].strip() for line in lines[body_start:] if line.startswith("# ") and line[2:].strip()), "")
    description = frontmatter.get("description", "").strip()
    if not description:
        description = next(
            (
                line.strip()
                for line in lines[body_start:]
                if line.strip() and not line.lstrip().startswith("#") and not line.lstrip().startswith("```")
            ),
            "",
        )
    name = frontmatter.get("name", "").strip() or heading or fallback_name
    return SkillManifest(
        slug=_safe_slug(frontmatter.get("name", "") or fallback_name),
        name=name[:160],
        description=description[:10_000],
        version=(frontmatter.get("version") or "").strip()[:80] or None,
    )


def _collect_safe_files(source_root: Path) -> list[tuple[Path, Path, int]]:
    source_root = source_root.resolve(strict=True)
    if not source_root.is_dir():
        raise HTTPException(status_code=422, detail="Skill source must be a directory")
    if (source_root / "SKILL.md").is_symlink() or not (source_root / "SKILL.md").is_file():
        raise HTTPException(status_code=422, detail="Skill source must contain a regular SKILL.md file")

    files: list[tuple[Path, Path, int]] = []
    total = 0
    for candidate in sorted(source_root.rglob("*")):
        relative = candidate.relative_to(source_root)
        if ".git" in relative.parts:
            continue
        if candidate.is_symlink() or not _is_within(candidate.resolve(), source_root):
            raise HTTPException(status_code=422, detail=f"Skill source contains an unsafe link: {relative.as_posix()}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise HTTPException(status_code=422, detail=f"Skill source contains an unsupported entry: {relative.as_posix()}")
        size = candidate.stat().st_size
        if size > MAX_SKILL_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"Skill file is too large: {relative.as_posix()}")
        total += size
        if total > MAX_SKILL_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="Skill package is too large")
        files.append((candidate, relative, size))
        if len(files) > MAX_SKILL_FILES:
            raise HTTPException(status_code=413, detail="Skill package has too many files")
    return files


def _copy_skill_directory(source_root: Path, destination_root: Path) -> list[tuple[Path, Path, int]]:
    files = _collect_safe_files(source_root)
    destination_root.mkdir(parents=True, exist_ok=False)
    for source, relative, _size in files:
        destination = destination_root / relative
        if not _is_within(destination.resolve(strict=False), destination_root.resolve()):
            raise HTTPException(status_code=422, detail="Skill contains an unsafe destination path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return files


def install_local_skill(
    db: Session,
    source_path: str | Path,
    *,
    source: str = "local",
    source_url: str | None = None,
    preferred_slug: str | None = None,
) -> Skill:
    """Copy one verified local Skill package into PGAgent-managed storage."""

    raw_source = Path(source_path).expanduser()
    try:
        source_root = raw_source.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"Skill source path is unavailable: {exc}") from exc
    if not source_root.is_dir():
        raise HTTPException(status_code=422, detail="Skill source must be a directory")
    manifest = parse_skill_manifest(source_root / "SKILL.md", fallback_name=source_root.name)
    slug = _safe_slug(preferred_slug) if preferred_slug else manifest.slug
    existing = db.scalar(select(Skill).where(Skill.slug == slug))
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"A Skill with slug '{slug}' is already installed")

    managed_root = _skills_root().resolve()
    managed_root.mkdir(parents=True, exist_ok=True)
    destination = managed_root / slug
    if destination.exists():
        raise HTTPException(status_code=409, detail=f"Managed Skill directory '{slug}' already exists")
    staging = managed_root / f".{slug}.{uuid.uuid4().hex}.staging"
    try:
        _copy_skill_directory(source_root, staging)
        staging.replace(destination)
        item = Skill(
            slug=slug,
            name=manifest.name,
            description=manifest.description,
            source=source,
            source_url=source_url,
            root_path=str(destination),
            version=manifest.version,
            enabled=True,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return item
    except HTTPException:
        db.rollback()
        if staging.exists():
            shutil.rmtree(staging)
        raise
    except Exception:
        db.rollback()
        if staging.exists():
            shutil.rmtree(staging)
        if destination.exists():
            shutil.rmtree(destination)
        raise


def uninstall_skill(db: Session, skill_id: str) -> None:
    """Remove one managed Skill package and detach it from live configurations."""

    item = db.get(Skill, skill_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    managed_root = _skills_root().resolve()
    skill_root = Path(item.root_path).resolve(strict=False)
    if skill_root == managed_root or not _is_within(skill_root, managed_root):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Skill files are outside PGAgent-managed storage and cannot be deleted",
        )
    if skill_root.exists() and (not skill_root.is_dir() or skill_root.is_symlink()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Managed Skill path is not a directory")

    staged_root: Path | None = None
    if skill_root.exists():
        staged_root = managed_root / f".{item.slug}.{uuid.uuid4().hex}.deleting"
        skill_root.replace(staged_root)
    try:
        db.execute(delete(AgentSkill).where(AgentSkill.skill_id == skill_id))
        db.execute(delete(SessionSkill).where(SessionSkill.skill_id == skill_id))
        db.delete(item)
        db.commit()
    except Exception:
        db.rollback()
        if staged_root is not None and staged_root.exists() and not skill_root.exists():
            staged_root.replace(skill_root)
        raise

    if staged_root is not None and staged_root.exists():
        shutil.rmtree(staged_root)


def _static_market_token() -> str | None:
    return os.getenv("SKILLS_SH_API_TOKEN") or os.getenv("PGAGENT_SKILLS_SH_API_TOKEN")


def _market_token() -> str | None:
    return _static_market_token() or os.getenv("VERCEL_OIDC_TOKEN")


def _read_local_oidc_token() -> str | None:
    """Read only VERCEL_OIDC_TOKEN from the CLI-managed local environment file."""

    try:
        lines = LOCAL_ENV_FILE.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in reversed(lines):
        key, separator, value = line.partition("=")
        if separator and key.strip() == "VERCEL_OIDC_TOKEN":
            token = value.strip()
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
                token = token[1:-1]
            return token or None
    return None


def _refresh_vercel_oidc_token(*, failed_token: str | None = None) -> str:
    """Refresh the local development OIDC token without exposing its value."""

    with _MARKET_TOKEN_REFRESH_LOCK:
        current = os.getenv("VERCEL_OIDC_TOKEN")
        if failed_token and current and current != failed_token:
            return current
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(SKILL_TOKEN_REFRESH_SCRIPT),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(
                status_code=502,
                detail="skills.sh marketplace token refresh could not be started",
            ) from exc
        if result.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail="skills.sh marketplace token refresh failed; run vercel login and retry",
            )
        token = _read_local_oidc_token()
        if not token or token == failed_token:
            raise HTTPException(
                status_code=502,
                detail="skills.sh marketplace token refresh did not return a fresh token",
            )
        os.environ["VERCEL_OIDC_TOKEN"] = token
        return token


def market_status() -> tuple[bool, str | None]:
    if _market_token():
        return True, None
    return False, "配置 SKILLS_SH_API_TOKEN（skills.sh 所需的 Bearer/OIDC 令牌）后可搜索市场。"


def _skills_sh_json(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    token = _market_token()
    if not token:
        raise HTTPException(status_code=409, detail="skills.sh marketplace token is not configured")

    def request(current_token: str) -> httpx.Response:
        return httpx.get(
            f"{SKILLS_SH_BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {current_token}", "Accept": "application/json"},
            timeout=10.0,
            follow_redirects=False,
        )

    try:
        response = request(token)
        if response.status_code == 401 and not _static_market_token():
            response = request(_refresh_vercel_oidc_token(failed_token=token))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"skills.sh marketplace request failed: {exc}") from exc
    if response.status_code == 401:
        raise HTTPException(status_code=502, detail="skills.sh rejected the configured marketplace token")
    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="skills.sh marketplace rate limit reached; retry later")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"skills.sh marketplace returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="skills.sh marketplace returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="skills.sh marketplace returned an invalid payload")
    return payload


def _market_item(row: dict[str, Any], *, official_owner: str | None = None) -> dict[str, Any] | None:
    """Expose only inert marketplace metadata to the browser.

    In particular, never proxy a marketplace file body or an arbitrary install
    command here.  File contents remain available only through the explicit
    preview-before-confirmation install flow.
    """

    if not isinstance(row.get("id"), str):
        return None
    source_url = row.get("installUrl") if isinstance(row.get("installUrl"), str) else None
    market_url = row.get("url") if isinstance(row.get("url"), str) else None
    installs = row.get("installs")
    change = row.get("change")
    installs_yesterday = row.get("installsYesterday")
    return {
        "id": row["id"],
        "slug": str(row.get("slug") or row["id"].rsplit("/", 1)[-1]),
        "name": str(row.get("name") or row.get("slug") or row["id"]),
        "source": str(row.get("source") or "skills.sh"),
        "source_url": source_url,
        "market_url": market_url,
        "installs": installs if isinstance(installs, int) else None,
        "change": change if isinstance(change, int) else None,
        "installs_yesterday": installs_yesterday if isinstance(installs_yesterday, int) else None,
        "is_official": official_owner is not None,
        "official_owner": official_owner,
        "is_duplicate": row.get("isDuplicate") is True,
    }


def _market_items(rows: Any, *, official_owner: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="skills.sh marketplace response has no data list")
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _market_item(row, official_owner=official_owner)
        if item is not None:
            items.append(item)
    return items


def search_market(query: str, limit: int) -> tuple[bool, str | None, list[dict[str, Any]]]:
    available, message = market_status()
    if not available:
        return False, message, []
    payload = _skills_sh_json("/api/v1/skills/search", params={"q": query, "limit": limit})
    items = _market_items(payload.get("data"))
    return True, None, items


def browse_market(view: str, *, page: int, per_page: int) -> tuple[bool, str | None, list[dict[str, Any]], bool, int | None]:
    """Read a leaderboard or curated listing without weakening install checks."""

    if view not in MARKET_BROWSE_VIEWS:
        raise HTTPException(status_code=422, detail="Unsupported marketplace view")
    available, message = market_status()
    if not available:
        return False, message, [], False, None

    if view == "curated":
        payload = _skills_sh_json("/api/v1/skills/curated")
        owners = payload.get("data")
        if not isinstance(owners, list):
            raise HTTPException(status_code=502, detail="skills.sh curated response has no data list")
        all_items: list[dict[str, Any]] = []
        for owner in owners:
            if not isinstance(owner, dict):
                continue
            owner_name = owner.get("owner") if isinstance(owner.get("owner"), str) else None
            all_items.extend(_market_items(owner.get("skills"), official_owner=owner_name))
        start = page * per_page
        selected = all_items[start : start + per_page]
        return True, None, selected, start + per_page < len(all_items), len(all_items)

    payload = _skills_sh_json(
        "/api/v1/skills",
        params={"view": view, "page": page, "per_page": per_page},
    )
    items = _market_items(payload.get("data"))
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        pagination = {}
    has_more = pagination.get("hasMore") is True
    total = pagination.get("total")
    return True, None, items, has_more, total if isinstance(total, int) else None


def _leaderboard_items(rows: Any) -> list[dict[str, Any]]:
    """Normalize, deduplicate, and rank one category's search results.

    The skills.sh search API ranks by relevance.  A category leaderboard instead
    needs a stable popularity order, so it uses that API only as a candidate
    source and orders the safe, normalized metadata locally by install count.
    """

    by_id: dict[str, dict[str, Any]] = {}
    for item in _market_items(rows):
        item_id = item["id"]
        previous = by_id.get(item_id)
        installs = item["installs"] if isinstance(item["installs"], int) else -1
        previous_installs = (
            previous["installs"] if previous is not None and isinstance(previous["installs"], int) else -1
        )
        if previous is None or installs > previous_installs:
            by_id[item_id] = item
    return sorted(
        by_id.values(),
        key=lambda item: (
            -(item["installs"] if isinstance(item["installs"], int) else -1),
            -(item["change"] if isinstance(item["change"], int) else -1),
            item["name"].casefold(),
            item["id"],
        ),
    )[:MARKET_LEADERBOARD_LIMIT]


def _build_market_leaderboards() -> tuple[dict[str, Any], ...]:
    categories: list[dict[str, Any]] = []
    for definition in MARKET_LEADERBOARD_CATEGORIES:
        payload = _skills_sh_json(
            "/api/v1/skills/search",
            params={"q": definition.query, "limit": MARKET_LEADERBOARD_SEARCH_LIMIT},
        )
        categories.append(
            {
                "id": definition.id,
                "name": definition.name,
                "description": definition.description,
                "items": _leaderboard_items(payload.get("data")),
            }
        )
    return tuple(categories)


def _market_leaderboard_snapshot(*, refresh: bool) -> tuple[_MarketLeaderboardCache, bool]:
    """Return a process-local cache snapshot, rebuilding it after its TTL.

    The external API itself caches short-lived search results.  This broader
    30-minute cache prevents five semantic searches per page visit while a
    manual refresh can still fetch a current snapshot immediately.
    """

    global _market_leaderboard_cache
    now_monotonic = time.monotonic()
    cached = _market_leaderboard_cache
    if not refresh and cached is not None and now_monotonic < cached.expires_at_monotonic:
        return cached, True

    with _market_leaderboard_lock:
        now_monotonic = time.monotonic()
        cached = _market_leaderboard_cache
        if not refresh and cached is not None and now_monotonic < cached.expires_at_monotonic:
            return cached, True

        refreshed_at = datetime.now(timezone.utc)
        expires_at = refreshed_at + timedelta(seconds=MARKET_LEADERBOARD_TTL_SECONDS)
        snapshot = _MarketLeaderboardCache(
            categories=_build_market_leaderboards(),
            refreshed_at=refreshed_at,
            expires_at=expires_at,
            expires_at_monotonic=now_monotonic + MARKET_LEADERBOARD_TTL_SECONDS,
        )
        _market_leaderboard_cache = snapshot
        return snapshot, False


def market_leaderboards(*, refresh: bool = False) -> dict[str, Any]:
    """Return the five fixed, popularity-ranked marketplace categories.

    This deliberately exposes only the same inert metadata as browse/search;
    it never returns marketplace file contents, install commands, or the
    bearer/OIDC credential used for the upstream request.
    """

    available, message = market_status()
    if not available:
        return {
            "available": False,
            "message": message,
            "categories": [],
            "refreshed_at": None,
            "expires_at": None,
            "ttl_seconds": MARKET_LEADERBOARD_TTL_SECONDS,
            "cached": False,
        }

    snapshot, cached = _market_leaderboard_snapshot(refresh=refresh)
    return {
        "available": True,
        "message": None,
        "categories": deepcopy(list(snapshot.categories)),
        "refreshed_at": snapshot.refreshed_at,
        "expires_at": snapshot.expires_at,
        "ttl_seconds": MARKET_LEADERBOARD_TTL_SECONDS,
        "cached": cached,
    }


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if (
        not value
        or "\x00" in value
        or path.is_absolute()
        or ".." in path.parts
        or any(":" in part for part in path.parts)
        or path == PurePosixPath(".")
    ):
        raise HTTPException(status_code=422, detail=f"Unsafe skill file path: {value}")
    return path


def _write_market_files(files: Sequence[dict[str, Any]], root: Path) -> None:
    if len(files) > MAX_SKILL_FILES:
        raise HTTPException(status_code=413, detail="Skill package has too many files")
    total = 0
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not isinstance(row.get("contents"), str):
            raise HTTPException(status_code=502, detail="skills.sh detail contains an invalid file entry")
        relative = _safe_relative_path(row["path"])
        contents = row["contents"].encode("utf-8")
        if len(contents) > MAX_SKILL_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"Skill file is too large: {relative.as_posix()}")
        total += len(contents)
        if total > MAX_SKILL_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="Skill package is too large")
        target = root.joinpath(*relative.parts)
        if not _is_within(target.resolve(strict=False), root.resolve()):
            raise HTTPException(status_code=422, detail="Skill contains an unsafe destination path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)


def _validate_market_id(market_id: str) -> str:
    normalized = market_id.strip().strip("/")
    parts = normalized.split("/")
    if len(parts) < 2 or any(not _SAFE_GITHUB_PART_RE.fullmatch(part) for part in parts):
        raise HTTPException(status_code=422, detail="market_id must be a safe skills.sh identifier")
    return normalized


def preview_market_skill(market_id: str, *, confirm: bool, db: Session) -> tuple[SkillPreview, Skill | None]:
    normalized = _validate_market_id(market_id)
    payload = _skills_sh_json(f"/api/v1/skills/{quote(normalized, safe='/')}")
    files = payload.get("files")
    if not isinstance(files, list):
        raise HTTPException(status_code=502, detail="skills.sh does not expose a file snapshot for this Skill")
    preview_files: list[tuple[str, int]] = []
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not isinstance(row.get("contents"), str):
            raise HTTPException(status_code=502, detail="skills.sh detail contains an invalid file entry")
        relative = _safe_relative_path(row["path"])
        preview_files.append((relative.as_posix(), len(row["contents"].encode("utf-8"))))
    source_url = f"https://skills.sh/{normalized}"
    preview = SkillPreview(source_url=source_url, candidates=(".",), files=tuple(preview_files), selected_root=None)
    if not confirm:
        return preview, None
    with tempfile.TemporaryDirectory(prefix="pgagent-market-") as temp_dir:
        source_root = Path(temp_dir)
        _write_market_files(files, source_root)
        installed = install_local_skill(
            db,
            source_root,
            source="skills_sh",
            source_url=source_url,
            preferred_slug=str(payload.get("slug") or normalized.rsplit("/", 1)[-1]),
        )
    return preview, installed


def _validate_github_url(url: str) -> tuple[str, list[str]]:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in GITHUB_ALLOWED_HOSTS or parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Only public HTTPS GitHub repository or ZIP URLs are supported")
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail="GitHub source URL has an unsafe path")
    return host, parts


def _github_default_archive_url(source_url: str) -> str:
    host, parts = _validate_github_url(source_url)
    if host != "github.com" or len(parts) != 2 or not all(_SAFE_GITHUB_PART_RE.fullmatch(part) for part in parts):
        raise HTTPException(status_code=422, detail="Repository URL must be https://github.com/{owner}/{repo}")
    owner, repo = parts
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=10.0,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub repository lookup failed: {exc}") from exc
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Public GitHub repository was not found")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GitHub repository lookup returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="GitHub repository lookup returned invalid JSON") from exc
    branch = payload.get("default_branch") if isinstance(payload, dict) else None
    if not isinstance(branch, str) or not branch or not _SAFE_GITHUB_PART_RE.fullmatch(branch):
        raise HTTPException(status_code=502, detail="GitHub repository has no safe default branch")
    return f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/zipball/{quote(branch)}"


def _archive_url_for_source(source_url: str) -> str:
    host, parts = _validate_github_url(source_url)
    if host == "github.com" and len(parts) == 2:
        return _github_default_archive_url(source_url)
    path = urlparse(source_url).path.lower()
    is_codeload_zip = host == "codeload.github.com" and len(parts) >= 4 and parts[2] == "zip"
    if not path.endswith(".zip") and not is_codeload_zip:
        raise HTTPException(status_code=422, detail="GitHub archive URL must end in .zip")
    return source_url


def _download_github_archive(url: str) -> bytes:
    _validate_github_url(url)
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"Accept": "application/zip"}) as client:
            with client.stream("GET", url) as response:
                history = [*response.history, response]
                for hop in history:
                    parsed = urlparse(str(hop.url))
                    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in GITHUB_ALLOWED_HOSTS:
                        raise HTTPException(status_code=422, detail="GitHub archive redirected to an untrusted host")
                if response.status_code == 404:
                    raise HTTPException(status_code=404, detail="GitHub archive was not found")
                if response.status_code >= 400:
                    raise HTTPException(status_code=502, detail=f"GitHub archive returned HTTP {response.status_code}")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise HTTPException(status_code=413, detail="GitHub archive is too large")
                    chunks.append(chunk)
                return b"".join(chunks)
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub archive download failed: {exc}") from exc


def _extract_safe_zip(archive: bytes, root: Path) -> None:
    try:
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            infos = [item for item in bundle.infolist() if not item.is_dir()]
            if len(infos) > MAX_ARCHIVE_FILES:
                raise HTTPException(status_code=413, detail="GitHub archive has too many files")
            total = 0
            for info in infos:
                relative = _safe_relative_path(info.filename)
                unix_mode = (info.external_attr >> 16) & 0o170000
                if unix_mode == 0o120000:
                    raise HTTPException(status_code=422, detail=f"GitHub archive contains a symlink: {relative.as_posix()}")
                if info.file_size > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise HTTPException(status_code=413, detail=f"GitHub archive file is too large: {relative.as_posix()}")
                total += info.file_size
                if total > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise HTTPException(status_code=413, detail="GitHub archive expands beyond the safe inspection limit")
                target = root.joinpath(*relative.parts)
                if not _is_within(target.resolve(strict=False), root.resolve()):
                    raise HTTPException(status_code=422, detail="GitHub archive contains an unsafe path")
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info, "r") as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
    except HTTPException:
        raise
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=422, detail="GitHub source is not a valid ZIP archive") from exc


def _find_skill_directories(root: Path) -> list[Path]:
    return sorted(
        {path.parent.resolve() for path in root.rglob("SKILL.md") if path.is_file() and not path.is_symlink()},
        key=lambda path: path.as_posix(),
    )


def _relative_candidate(root: Path, candidate: Path) -> str:
    return candidate.relative_to(root).as_posix() or "."


def _select_skill_directory(root: Path, skill_path: str | None) -> tuple[list[Path], Path | None]:
    candidates = _find_skill_directories(root)
    if not candidates:
        raise HTTPException(status_code=422, detail="GitHub archive contains no SKILL.md folder")
    if skill_path:
        relative = _safe_relative_path(skill_path)
        selected = root.joinpath(*relative.parts).resolve()
        if not _is_within(selected, root.resolve()) or selected not in candidates:
            raise HTTPException(
                status_code=422,
                detail={"skill_path": "does not point to a SKILL.md folder", "candidates": [_relative_candidate(root, item) for item in candidates]},
            )
        return candidates, selected
    if len(candidates) == 1:
        return candidates, candidates[0]
    return candidates, None


def preview_github_skill(
    source_url: str,
    *,
    skill_path: str | None,
    confirm: bool,
    db: Session,
) -> tuple[SkillPreview, Skill | None]:
    archive_url = _archive_url_for_source(source_url)
    archive = _download_github_archive(archive_url)
    with tempfile.TemporaryDirectory(prefix="pgagent-github-") as temp_dir:
        extraction_root = Path(temp_dir)
        _extract_safe_zip(archive, extraction_root)
        candidates, selected = _select_skill_directory(extraction_root, skill_path)
        preview = SkillPreview(
            source_url=source_url,
            candidates=tuple(_relative_candidate(extraction_root, candidate) for candidate in candidates),
            files=tuple(
                (relative.as_posix(), size)
                for source, relative, size in _collect_safe_files(selected)
            )
            if selected is not None
            else (),
            selected_root=selected,
        )
        if not confirm:
            return preview, None
        if selected is None:
            raise HTTPException(
                status_code=409,
                detail={"message": "Choose skill_path before installing", "candidates": list(preview.candidates)},
            )
        installed = install_local_skill(db, selected, source="github", source_url=source_url)
        return preview, installed
