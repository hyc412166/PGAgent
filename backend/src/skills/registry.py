"""Safe persistence and transport helpers for PGAgent Skills and capabilities.

This module deliberately never imports or executes a file from an installed
Skill.  A Skill is configuration and copied source material until a later
runtime integration explicitly supports it.
"""
# 文件职责：负责技能发现、加载与注册中的 registry 子模块。
# 逻辑关系：上层通过 skills/registry.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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


# 变量说明：MAX_SKILL_FILES 表示当前流程使用的 MAX_SKILL_FILES 集合。
MAX_SKILL_FILES = 200
# 变量说明：MAX_SKILL_FILE_BYTES 表示当前流程使用的 MAX_SKILL_FILE_BYTES 集合。
MAX_SKILL_FILE_BYTES = 2 * 1024 * 1024
# 变量说明：MAX_SKILL_TOTAL_BYTES 表示当前流程使用的 MAX_SKILL_TOTAL_BYTES 集合。
MAX_SKILL_TOTAL_BYTES = 10 * 1024 * 1024
# 变量说明：MAX_ARCHIVE_BYTES 表示当前流程使用的 MAX_ARCHIVE_BYTES 集合。
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
# 变量说明：MAX_ARCHIVE_FILES 表示当前流程使用的 MAX_ARCHIVE_FILES 集合。
MAX_ARCHIVE_FILES = 5_000
# 变量说明：MAX_ARCHIVE_EXPANDED_BYTES 表示当前流程使用的 MAX_ARCHIVE_EXPANDED_BYTES 集合。
MAX_ARCHIVE_EXPANDED_BYTES = 50 * 1024 * 1024
# 变量说明：SKILLS_SH_BASE_URL 表示SKILLS_SH_BASE 的访问地址。
SKILLS_SH_BASE_URL = "https://skills.sh"
# 变量说明：PROJECT_ROOT 表示当前步骤使用的 PROJECT_ROOT 值。
PROJECT_ROOT = settings.data_dir.parent
# 变量说明：LOCAL_ENV_FILE 表示当前步骤使用的 LOCAL_ENV_FILE 值。
LOCAL_ENV_FILE = PROJECT_ROOT / ".env.local"
# 变量说明：SKILL_TOKEN_REFRESH_SCRIPT 表示当前步骤使用的 SKILL_TOKEN_REFRESH_SCRIPT 值。
SKILL_TOKEN_REFRESH_SCRIPT = PROJECT_ROOT / "scripts" / "refresh-skills-token.ps1"
# 变量说明：MARKET_BROWSE_VIEWS 表示当前流程使用的 MARKET_BROWSE_VIEWS 集合。
MARKET_BROWSE_VIEWS = frozenset({"all-time", "trending", "hot", "curated"})
# 变量说明：MARKET_LEADERBOARD_LIMIT 表示当前步骤使用的 MARKET_LEADERBOARD_LIMIT 值。
MARKET_LEADERBOARD_LIMIT = 6
# 变量说明：MARKET_LEADERBOARD_SEARCH_LIMIT 表示当前步骤使用的 MARKET_LEADERBOARD_SEARCH_LIMIT 值。
MARKET_LEADERBOARD_SEARCH_LIMIT = 50
# 变量说明：MARKET_LEADERBOARD_TTL_SECONDS 表示当前流程使用的 MARKET_LEADERBOARD_TTL_SECONDS 集合。
MARKET_LEADERBOARD_TTL_SECONDS = 30 * 60
# 变量说明：GITHUB_ALLOWED_HOSTS 表示当前流程使用的 GITHUB_ALLOWED_HOSTS 集合。
GITHUB_ALLOWED_HOSTS = frozenset({"github.com", "api.github.com", "codeload.github.com"})
# 变量说明：_SLUG_RE 表示当前步骤使用的 _SLUG_RE 值。
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# 变量说明：_SAFE_GITHUB_PART_RE 表示当前步骤使用的 _SAFE_GITHUB_PART_RE 值。
_SAFE_GITHUB_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# 变量说明：_MARKET_TOKEN_REFRESH_LOCK 表示当前步骤使用的 _MARKET_TOKEN_REFRESH_LOCK 值。
_MARKET_TOKEN_REFRESH_LOCK = threading.Lock()


# 类职责：定义 MarketLeaderboardDefinition 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class MarketLeaderboardDefinition:
    """A user-facing Skills marketplace category backed by semantic search."""

    # 变量说明：id 表示当前对象的唯一标识。
    id: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：query 表示当前步骤使用的 query 值。
    query: str


# 变量说明：MARKET_LEADERBOARD_CATEGORIES 表示当前流程使用的 MARKET_LEADERBOARD_CATEGORIES 集合。
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


# 类职责：定义 _MarketLeaderboardCache 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class _MarketLeaderboardCache:
    # 变量说明：categories 表示当前流程使用的 categories 集合。
    categories: tuple[dict[str, Any], ...]
    # 变量说明：refreshed_at 表示refreshed_at 对应的时间信息。
    refreshed_at: datetime
    # 变量说明：expires_at 表示expires_at 对应的时间信息。
    expires_at: datetime
    # 变量说明：expires_at_monotonic 表示当前步骤使用的 expires_at_monotonic 值。
    expires_at_monotonic: float


# 变量说明：_market_leaderboard_cache 表示当前步骤使用的 _market_leaderboard_cache 值。
_market_leaderboard_cache: _MarketLeaderboardCache | None = None
# 变量说明：_market_leaderboard_lock 表示当前步骤使用的 _market_leaderboard_lock 值。
_market_leaderboard_lock = threading.RLock()


# 类职责：定义 SkillManifest 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class SkillManifest:
    # 变量说明：slug 表示当前步骤使用的 slug 值。
    slug: str
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：version 表示当前步骤使用的 version 值。
    version: str | None


# 类职责：定义 SkillPreview 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class SkillPreview:
    # 变量说明：source_url 表示source 的访问地址。
    source_url: str
    # 变量说明：candidates 表示当前流程使用的 candidates 集合。
    candidates: tuple[str, ...]
    # 变量说明：files 表示当前流程使用的 files 集合。
    files: tuple[tuple[str, int], ...]
    # 变量说明：selected_root 表示当前步骤使用的 selected_root 值。
    selected_root: Path | None


# 函数职责：完成 tool_catalog_payload 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：完成 normalized_ids 对应的业务处理。
# 参数关系：values 表示当前流程使用的 values 集合；label 表示当前步骤使用的 label 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _normalized_ids(values: Iterable[str] | None, *, label: str) -> list[str]:
    # 变量说明：result 表示本步骤产生的结果。
    result: list[str] = []
    # 变量说明：seen 表示当前步骤使用的 seen 值。
    seen: set[str] = set()
    for value in values or []:
        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized = str(value).strip()
        if not normalized:
            raise HTTPException(status_code=422, detail=f"{label} must not contain blank values")
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


# 函数职责：校验 tool_ids 对应的数据或流程。
# 参数关系：tool_ids 表示tool 对象标识集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def validate_tool_ids(tool_ids: Iterable[str] | None) -> list[str]:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = _normalized_ids(tool_ids, label="tool_ids")
    # 变量说明：unknown 表示当前步骤使用的 unknown 值。
    unknown = [tool_id for tool_id in normalized if tool_id not in BUILTIN_TOOL_BY_ID]
    if unknown:
        raise HTTPException(status_code=422, detail={"unknown_tool_ids": unknown})
    return normalized


# 函数职责：校验 skill_ids 对应的数据或流程。
# 参数关系：db 表示当前数据库会话；skill_ids 表示skill 对象标识集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def validate_skill_ids(db: Session, skill_ids: Iterable[str] | None) -> list[str]:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = _normalized_ids(skill_ids, label="skill_ids")
    if not normalized:
        return []
    # 变量说明：rows 表示当前流程使用的 rows 集合。
    rows = list(db.scalars(select(Skill).where(Skill.id.in_(normalized))))
    # 变量说明：by_id 表示by 对象的唯一标识。
    by_id = {row.id: row for row in rows}
    # 变量说明：missing 表示当前步骤使用的 missing 值。
    missing = [skill_id for skill_id in normalized if skill_id not in by_id]
    if missing:
        raise HTTPException(status_code=422, detail={"unknown_skill_ids": missing})
    # 变量说明：disabled 表示当前步骤使用的 disabled 值。
    disabled = [skill_id for skill_id in normalized if not by_id[skill_id].enabled]
    if disabled:
        raise HTTPException(status_code=409, detail={"disabled_skill_ids": disabled})
    return normalized


# 函数职责：完成 replace_agent_capabilities 对应的业务处理。
# 参数关系：db 表示当前数据库会话；agent 表示当前步骤使用的 agent 值；tool_ids 表示tool 对象标识集合；skill_ids 表示skill 对象标识集合；replace_tools 表示当前流程使用的 replace_tools 集合；replace_skills 表示当前流程使用的 replace_skills 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
        # 变量说明：normalized_tools 表示当前流程使用的 normalized_tools 集合。
        normalized_tools = validate_tool_ids(tool_ids)
        db.execute(delete(AgentTool).where(AgentTool.agent_id == agent.id))
        db.add_all(AgentTool(agent_id=agent.id, tool_id=tool_id) for tool_id in normalized_tools)
    if replace_skills:
        # 变量说明：normalized_skills 表示当前流程使用的 normalized_skills 集合。
        normalized_skills = validate_skill_ids(db, skill_ids)
        db.execute(delete(AgentSkill).where(AgentSkill.agent_id == agent.id))
        db.add_all(AgentSkill(agent_id=agent.id, skill_id=skill_id) for skill_id in normalized_skills)


# 函数职责：完成 replace_session_skills 对应的业务处理。
# 参数关系：db 表示当前数据库会话；chat_session 表示当前步骤使用的 chat_session 值；skill_ids 表示skill 对象标识集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def replace_session_skills(
    db: Session,
    chat_session: ChatSession,
    skill_ids: Iterable[str] | None,
) -> None:
    # 变量说明：normalized_skills 表示当前流程使用的 normalized_skills 集合。
    normalized_skills = validate_skill_ids(db, skill_ids)
    db.execute(delete(SessionSkill).where(SessionSkill.session_id == chat_session.id))
    db.add_all(SessionSkill(session_id=chat_session.id, skill_id=skill_id) for skill_id in normalized_skills)


# 函数职责：完成 skills_root 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _skills_root() -> Path:
    return settings.data_dir / "skills"


# 函数职责：完成 is_within 对应的业务处理。
# 参数关系：path 表示当前文件或目录路径；root 表示处理范围的根目录。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# 函数职责：完成 safe_slug 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _safe_slug(value: str) -> str:
    # 变量说明：slug 表示当前步骤使用的 slug 值。
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    # 变量说明：slug 表示当前步骤使用的 slug 值。
    slug = slug[:100].strip("-")
    if not slug:
        raise HTTPException(status_code=422, detail="Skill name cannot produce a safe slug")
    return slug


# 函数职责：完成 strip_quotes 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _strip_quotes(value: str) -> str:
    # 变量说明：value 表示当前字段或计算值。
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


# 函数职责：解析 skill_manifest 对应的数据或流程。
# 参数关系：skill_file 表示当前步骤使用的 skill_file 值；fallback_name 表示当前步骤使用的 fallback_name 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def parse_skill_manifest(skill_file: Path, *, fallback_name: str) -> SkillManifest:
    """Extract conservative metadata without evaluating YAML or markdown code."""

    try:
        # 变量说明：text 表示当前步骤使用的 text 值。
        text = skill_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="SKILL.md must be UTF-8 text") from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"Cannot read SKILL.md: {exc}") from exc

    # 变量说明：lines 表示当前流程使用的 lines 集合。
    lines = text.splitlines()
    # 变量说明：frontmatter 表示当前步骤使用的 frontmatter 值。
    frontmatter: dict[str, str] = {}
    # 变量说明：body_start 表示当前步骤使用的 body_start 值。
    body_start = 0
    if lines and lines[0].strip() == "---":
        # 变量说明：closing 表示当前步骤使用的 closing 值。
        closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
        if closing is None:
            raise HTTPException(status_code=422, detail="SKILL.md frontmatter is not closed")
        for line in lines[1:closing]:
            # 变量说明：key 表示用于查找或映射的键；separator 表示当前步骤使用的 separator 值；value 表示当前字段或计算值。
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {"name", "description", "version"}:
                frontmatter[key.strip().lower()] = _strip_quotes(value)
        # 变量说明：body_start 表示当前步骤使用的 body_start 值。
        body_start = closing + 1

    # 变量说明：heading 表示当前步骤使用的 heading 值。
    heading = next((line[2:].strip() for line in lines[body_start:] if line.startswith("# ") and line[2:].strip()), "")
    # 变量说明：description 表示当前步骤使用的 description 值。
    description = frontmatter.get("description", "").strip()
    if not description:
        # 变量说明：description 表示当前步骤使用的 description 值。
        description = next(
            (
                line.strip()
                for line in lines[body_start:]
                if line.strip() and not line.lstrip().startswith("#") and not line.lstrip().startswith("```")
            ),
            "",
        )
    # 变量说明：name 表示当前对象名称。
    name = frontmatter.get("name", "").strip() or heading or fallback_name
    return SkillManifest(
        slug=_safe_slug(frontmatter.get("name", "") or fallback_name),
        name=name[:160],
        description=description[:10_000],
        version=(frontmatter.get("version") or "").strip()[:80] or None,
    )


# 函数职责：收集 safe_files 对应的数据或流程。
# 参数关系：source_root 表示当前步骤使用的 source_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _collect_safe_files(source_root: Path) -> list[tuple[Path, Path, int]]:
    # 变量说明：source_root 表示当前步骤使用的 source_root 值。
    source_root = source_root.resolve(strict=True)
    if not source_root.is_dir():
        raise HTTPException(status_code=422, detail="Skill source must be a directory")
    if (source_root / "SKILL.md").is_symlink() or not (source_root / "SKILL.md").is_file():
        raise HTTPException(status_code=422, detail="Skill source must contain a regular SKILL.md file")

    # 变量说明：files 表示当前流程使用的 files 集合。
    files: list[tuple[Path, Path, int]] = []
    # 变量说明：total 表示当前步骤使用的 total 值。
    total = 0
    for candidate in sorted(source_root.rglob("*")):
        # 变量说明：relative 表示当前步骤使用的 relative 值。
        relative = candidate.relative_to(source_root)
        if ".git" in relative.parts:
            continue
        if candidate.is_symlink() or not _is_within(candidate.resolve(), source_root):
            raise HTTPException(status_code=422, detail=f"Skill source contains an unsafe link: {relative.as_posix()}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise HTTPException(status_code=422, detail=f"Skill source contains an unsupported entry: {relative.as_posix()}")
        # 变量说明：size 表示当前步骤使用的 size 值。
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


# 函数职责：完成 copy_skill_directory 对应的业务处理。
# 参数关系：source_root 表示当前步骤使用的 source_root 值；destination_root 表示当前步骤使用的 destination_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _copy_skill_directory(source_root: Path, destination_root: Path) -> list[tuple[Path, Path, int]]:
    # 变量说明：files 表示当前流程使用的 files 集合。
    files = _collect_safe_files(source_root)
    destination_root.mkdir(parents=True, exist_ok=False)
    for source, relative, _size in files:
        # 变量说明：destination 表示当前步骤使用的 destination 值。
        destination = destination_root / relative
        if not _is_within(destination.resolve(strict=False), destination_root.resolve()):
            raise HTTPException(status_code=422, detail="Skill contains an unsafe destination path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return files


# 函数职责：完成 install_local_skill 对应的业务处理。
# 参数关系：db 表示当前数据库会话；source_path 表示source_path 对应的文件系统位置；source 表示当前步骤使用的 source 值；source_url 表示source 的访问地址；preferred_slug 表示当前步骤使用的 preferred_slug 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def install_local_skill(
    db: Session,
    source_path: str | Path,
    *,
    source: str = "local",
    source_url: str | None = None,
    preferred_slug: str | None = None,
) -> Skill:
    """Copy one verified local Skill package into PGAgent-managed storage."""

    # 变量说明：raw_source 表示当前步骤使用的 raw_source 值。
    raw_source = Path(source_path).expanduser()
    try:
        # 变量说明：source_root 表示当前步骤使用的 source_root 值。
        source_root = raw_source.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"Skill source path is unavailable: {exc}") from exc
    if not source_root.is_dir():
        raise HTTPException(status_code=422, detail="Skill source must be a directory")
    # 变量说明：manifest 表示当前步骤使用的 manifest 值。
    manifest = parse_skill_manifest(source_root / "SKILL.md", fallback_name=source_root.name)
    # 变量说明：slug 表示当前步骤使用的 slug 值。
    slug = _safe_slug(preferred_slug) if preferred_slug else manifest.slug
    # 变量说明：existing 表示当前步骤使用的 existing 值。
    existing = db.scalar(select(Skill).where(Skill.slug == slug))
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"A Skill with slug '{slug}' is already installed")

    # 变量说明：managed_root 表示当前步骤使用的 managed_root 值。
    managed_root = _skills_root().resolve()
    managed_root.mkdir(parents=True, exist_ok=True)
    # 变量说明：destination 表示当前步骤使用的 destination 值。
    destination = managed_root / slug
    if destination.exists():
        raise HTTPException(status_code=409, detail=f"Managed Skill directory '{slug}' already exists")
    # 变量说明：staging 表示当前步骤使用的 staging 值。
    staging = managed_root / f".{slug}.{uuid.uuid4().hex}.staging"
    try:
        _copy_skill_directory(source_root, staging)
        staging.replace(destination)
        # 变量说明：item 表示当前步骤使用的 item 值。
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


# 函数职责：完成 uninstall_skill 对应的业务处理。
# 参数关系：db 表示当前数据库会话；skill_id 表示skill 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def uninstall_skill(db: Session, skill_id: str) -> None:
    """Remove one managed Skill package and detach it from live configurations."""

    # 变量说明：item 表示当前步骤使用的 item 值。
    item = db.get(Skill, skill_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    # 变量说明：managed_root 表示当前步骤使用的 managed_root 值。
    managed_root = _skills_root().resolve()
    # 变量说明：skill_root 表示当前步骤使用的 skill_root 值。
    skill_root = Path(item.root_path).resolve(strict=False)
    if skill_root == managed_root or not _is_within(skill_root, managed_root):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Skill files are outside PGAgent-managed storage and cannot be deleted",
        )
    if skill_root.exists() and (not skill_root.is_dir() or skill_root.is_symlink()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Managed Skill path is not a directory")

    # 变量说明：staged_root 表示当前步骤使用的 staged_root 值。
    staged_root: Path | None = None
    if skill_root.exists():
        # 变量说明：staged_root 表示当前步骤使用的 staged_root 值。
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


# 函数职责：完成 static_market_token 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _static_market_token() -> str | None:
    return os.getenv("SKILLS_SH_API_TOKEN") or os.getenv("PGAGENT_SKILLS_SH_API_TOKEN")


# 函数职责：完成 market_gateway 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _market_gateway() -> tuple[str, str] | None:
    # 变量说明：base_url 表示base 的访问地址。
    base_url = (os.getenv("PGAGENT_SKILL_MARKET_URL") or "").strip().rstrip("/")
    # 变量说明：client_token 表示当前步骤使用的 client_token 值。
    client_token = (os.getenv("PGAGENT_SKILL_MARKET_CLIENT_TOKEN") or "").strip()
    if not base_url and not client_token:
        return None
    if not base_url or not client_token:
        raise HTTPException(
            status_code=409,
            detail="PGAGENT_SKILL_MARKET_URL and PGAGENT_SKILL_MARKET_CLIENT_TOKEN must be configured together",
        )
    # 变量说明：parsed 表示当前步骤使用的 parsed 值。
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=409, detail="PGAGENT_SKILL_MARKET_URL must be an HTTPS origin or base path")
    return base_url, client_token


# 函数职责：完成 market_token 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _market_token() -> str | None:
    return _static_market_token() or os.getenv("VERCEL_OIDC_TOKEN")


# 函数职责：完成 read_local_oidc_token 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _read_local_oidc_token() -> str | None:
    """Read only VERCEL_OIDC_TOKEN from the CLI-managed local environment file."""

    try:
        # 变量说明：lines 表示当前流程使用的 lines 集合。
        lines = LOCAL_ENV_FILE.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in reversed(lines):
        # 变量说明：key 表示用于查找或映射的键；separator 表示当前步骤使用的 separator 值；value 表示当前字段或计算值。
        key, separator, value = line.partition("=")
        if separator and key.strip() == "VERCEL_OIDC_TOKEN":
            # 变量说明：token 表示当前步骤使用的 token 值。
            token = value.strip()
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
                # 变量说明：token 表示当前步骤使用的 token 值。
                token = token[1:-1]
            return token or None
    return None


# 函数职责：完成 refresh_vercel_oidc_token 对应的业务处理。
# 参数关系：failed_token 表示当前步骤使用的 failed_token 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _refresh_vercel_oidc_token(*, failed_token: str | None = None) -> str:
    """Refresh the local development OIDC token without exposing its value."""

    with _MARKET_TOKEN_REFRESH_LOCK:
        # 变量说明：current 表示当前步骤使用的 current 值。
        current = os.getenv("VERCEL_OIDC_TOKEN")
        if failed_token and current and current != failed_token:
            return current
        try:
            # 变量说明：result 表示本步骤产生的结果。
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
        # 变量说明：token 表示当前步骤使用的 token 值。
        token = _read_local_oidc_token()
        if not token or token == failed_token:
            raise HTTPException(
                status_code=502,
                detail="skills.sh marketplace token refresh did not return a fresh token",
            )
        # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
        os.environ["VERCEL_OIDC_TOKEN"] = token
        return token


# 函数职责：完成 market_status 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def market_status() -> tuple[bool, str | None]:
    try:
        if _market_gateway() is not None:
            return True, None
    except HTTPException as exc:
        return False, str(exc.detail)
    if _market_token():
        return True, None
    return False, "配置 PGAGENT_SKILL_MARKET_URL 和客户端令牌后可使用长期在线市场。"


# 函数职责：完成 skills_sh_json 对应的业务处理。
# 参数关系：path 表示当前文件或目录路径；params 表示当前流程使用的 params 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _skills_sh_json(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    # 变量说明：gateway 表示当前步骤使用的 gateway 值。
    gateway = _market_gateway()
    if gateway is not None:
        # 变量说明：base_url 表示base 的访问地址；client_token 表示当前步骤使用的 client_token 值。
        base_url, client_token = gateway
        # 变量说明：prefix 表示当前步骤使用的 prefix 值。
        prefix = "/api/v1/"
        if not path.startswith(prefix):
            raise HTTPException(status_code=500, detail="Unsupported skills.sh API path")
        # 变量说明：request_url 表示request 的访问地址。
        request_url = f"{base_url}/api/market/{path.removeprefix(prefix)}"
        try:
            # 变量说明：response 表示下游返回的响应。
            response = httpx.get(
                request_url,
                params=params,
                headers={"Authorization": f"Bearer {client_token}", "Accept": "application/json"},
                timeout=25.0,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Skill marketplace gateway request failed: {exc}") from exc
        if response.status_code == 401:
            raise HTTPException(status_code=502, detail="Skill marketplace gateway rejected the configured client token")
        if response.status_code == 429:
            raise HTTPException(status_code=429, detail="Skill marketplace rate limit reached; retry later")
        if response.status_code == 502:
            try:
                # 变量说明：gateway_error 表示当前步骤使用的 gateway_error 值。
                gateway_error = response.json()
            except ValueError:
                # 变量说明：gateway_error 表示当前步骤使用的 gateway_error 值。
                gateway_error = None
            if isinstance(gateway_error, dict) and gateway_error.get("error") == "oidc_rejected":
                raise HTTPException(status_code=502, detail="skills.sh rejected the gateway OIDC token")
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Skill marketplace gateway returned HTTP {response.status_code}")
        try:
            # 变量说明：payload 表示跨层传递的数据载荷。
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="Skill marketplace gateway returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=502, detail="Skill marketplace gateway returned an invalid payload")
        return payload

    # 变量说明：token 表示当前步骤使用的 token 值。
    token = _market_token()
    if not token:
        raise HTTPException(status_code=409, detail="skills.sh marketplace token is not configured")

    # 函数职责：完成 request 对应的业务处理。
    # 参数关系：current_token 表示当前步骤使用的 current_token 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def request(current_token: str) -> httpx.Response:
        return httpx.get(
            f"{SKILLS_SH_BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {current_token}", "Accept": "application/json"},
            timeout=10.0,
            follow_redirects=False,
        )

    try:
        # 变量说明：response 表示下游返回的响应。
        response = request(token)
        if response.status_code == 401 and not _static_market_token():
            # 变量说明：response 表示下游返回的响应。
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
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="skills.sh marketplace returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="skills.sh marketplace returned an invalid payload")
    return payload


# 函数职责：完成 market_item 对应的业务处理。
# 参数关系：row 表示当前步骤使用的 row 值；official_owner 表示当前步骤使用的 official_owner 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _market_item(row: dict[str, Any], *, official_owner: str | None = None) -> dict[str, Any] | None:
    """Expose only inert marketplace metadata to the browser.

    In particular, never proxy a marketplace file body or an arbitrary install
    command here.  File contents remain available only through the explicit
    preview-before-confirmation install flow.
    """

    if not isinstance(row.get("id"), str):
        return None
    # 变量说明：source_url 表示source 的访问地址。
    source_url = row.get("installUrl") if isinstance(row.get("installUrl"), str) else None
    # 变量说明：market_url 表示market 的访问地址。
    market_url = row.get("url") if isinstance(row.get("url"), str) else None
    # 变量说明：installs 表示当前流程使用的 installs 集合。
    installs = row.get("installs")
    # 变量说明：change 表示当前步骤使用的 change 值。
    change = row.get("change")
    # 变量说明：installs_yesterday 表示当前步骤使用的 installs_yesterday 值。
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


# 函数职责：完成 market_items 对应的业务处理。
# 参数关系：rows 表示当前流程使用的 rows 集合；official_owner 表示当前步骤使用的 official_owner 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _market_items(rows: Any, *, official_owner: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="skills.sh marketplace response has no data list")
    # 变量说明：items 表示待处理的元素集合。
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # 变量说明：item 表示当前步骤使用的 item 值。
        item = _market_item(row, official_owner=official_owner)
        if item is not None:
            items.append(item)
    return items


# 函数职责：完成 search_market 对应的业务处理。
# 参数关系：query 表示当前步骤使用的 query 值；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def search_market(query: str, limit: int) -> tuple[bool, str | None, list[dict[str, Any]]]:
    # 变量说明：available 表示当前步骤使用的 available 值；message 表示当前消息。
    available, message = market_status()
    if not available:
        return False, message, []
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload = _skills_sh_json("/api/v1/skills/search", params={"q": query, "limit": limit})
    # 变量说明：items 表示待处理的元素集合。
    items = _market_items(payload.get("data"))
    return True, None, items


# 函数职责：完成 browse_market 对应的业务处理。
# 参数关系：view 表示当前步骤使用的 view 值；page 表示当前步骤使用的 page 值；per_page 表示当前步骤使用的 per_page 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def browse_market(view: str, *, page: int, per_page: int) -> tuple[bool, str | None, list[dict[str, Any]], bool, int | None]:
    """Read a leaderboard or curated listing without weakening install checks."""

    if view not in MARKET_BROWSE_VIEWS:
        raise HTTPException(status_code=422, detail="Unsupported marketplace view")
    # 变量说明：available 表示当前步骤使用的 available 值；message 表示当前消息。
    available, message = market_status()
    if not available:
        return False, message, [], False, None

    if view == "curated":
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = _skills_sh_json("/api/v1/skills/curated")
        # 变量说明：owners 表示当前流程使用的 owners 集合。
        owners = payload.get("data")
        if not isinstance(owners, list):
            raise HTTPException(status_code=502, detail="skills.sh curated response has no data list")
        # 变量说明：all_items 表示当前流程使用的 all_items 集合。
        all_items: list[dict[str, Any]] = []
        for owner in owners:
            if not isinstance(owner, dict):
                continue
            # 变量说明：owner_name 表示当前步骤使用的 owner_name 值。
            owner_name = owner.get("owner") if isinstance(owner.get("owner"), str) else None
            all_items.extend(_market_items(owner.get("skills"), official_owner=owner_name))
        # 变量说明：start 表示当前步骤使用的 start 值。
        start = page * per_page
        # 变量说明：selected 表示当前步骤使用的 selected 值。
        selected = all_items[start : start + per_page]
        return True, None, selected, start + per_page < len(all_items), len(all_items)

    # 变量说明：payload 表示跨层传递的数据载荷。
    payload = _skills_sh_json(
        "/api/v1/skills",
        params={"view": view, "page": page, "per_page": per_page},
    )
    # 变量说明：items 表示待处理的元素集合。
    items = _market_items(payload.get("data"))
    # 变量说明：pagination 表示当前步骤使用的 pagination 值。
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        # 变量说明：pagination 表示当前步骤使用的 pagination 值。
        pagination = {}
    # 变量说明：has_more 表示表示是否满足 _more 条件的布尔标记。
    has_more = pagination.get("hasMore") is True
    # 变量说明：total 表示当前步骤使用的 total 值。
    total = pagination.get("total")
    return True, None, items, has_more, total if isinstance(total, int) else None


# 函数职责：完成 leaderboard_items 对应的业务处理。
# 参数关系：rows 表示当前流程使用的 rows 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _leaderboard_items(rows: Any) -> list[dict[str, Any]]:
    """Normalize, deduplicate, and rank one category's search results.

    The skills.sh search API ranks by relevance.  A category leaderboard instead
    needs a stable popularity order, so it uses that API only as a candidate
    source and orders the safe, normalized metadata locally by install count.
    """

    # 变量说明：by_id 表示by 对象的唯一标识。
    by_id: dict[str, dict[str, Any]] = {}
    for item in _market_items(rows):
        # 变量说明：item_id 表示item 对象的唯一标识。
        item_id = item["id"]
        # 变量说明：previous 表示当前流程使用的 previous 集合。
        previous = by_id.get(item_id)
        # 变量说明：installs 表示当前流程使用的 installs 集合。
        installs = item["installs"] if isinstance(item["installs"], int) else -1
        # 变量说明：previous_installs 表示当前流程使用的 previous_installs 集合。
        previous_installs = (
            previous["installs"] if previous is not None and isinstance(previous["installs"], int) else -1
        )
        if previous is None or installs > previous_installs:
            # 变量说明：by_id 的索引项 表示该语句创建或更新的目标数据。
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


# 函数职责：构建 market_leaderboards 对应的数据或流程。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _build_market_leaderboards() -> tuple[dict[str, Any], ...]:
    # 变量说明：categories 表示当前流程使用的 categories 集合。
    categories: list[dict[str, Any]] = []
    for definition in MARKET_LEADERBOARD_CATEGORIES:
        # 变量说明：payload 表示跨层传递的数据载荷。
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


# 函数职责：完成 market_leaderboard_snapshot 对应的业务处理。
# 参数关系：refresh 表示当前步骤使用的 refresh 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _market_leaderboard_snapshot(*, refresh: bool) -> tuple[_MarketLeaderboardCache, bool]:
    """Return a process-local cache snapshot, rebuilding it after its TTL.

    The external API itself caches short-lived search results.  This broader
    30-minute cache prevents five semantic searches per page visit while a
    manual refresh can still fetch a current snapshot immediately.
    """

    global _market_leaderboard_cache
    # 变量说明：now_monotonic 表示当前步骤使用的 now_monotonic 值。
    now_monotonic = time.monotonic()
    # 变量说明：cached 表示当前步骤使用的 cached 值。
    cached = _market_leaderboard_cache
    if not refresh and cached is not None and now_monotonic < cached.expires_at_monotonic:
        return cached, True

    with _market_leaderboard_lock:
        # 变量说明：now_monotonic 表示当前步骤使用的 now_monotonic 值。
        now_monotonic = time.monotonic()
        # 变量说明：cached 表示当前步骤使用的 cached 值。
        cached = _market_leaderboard_cache
        if not refresh and cached is not None and now_monotonic < cached.expires_at_monotonic:
            return cached, True

        # 变量说明：refreshed_at 表示refreshed_at 对应的时间信息。
        refreshed_at = datetime.now(timezone.utc)
        # 变量说明：expires_at 表示expires_at 对应的时间信息。
        expires_at = refreshed_at + timedelta(seconds=MARKET_LEADERBOARD_TTL_SECONDS)
        # 变量说明：snapshot 表示当前步骤使用的 snapshot 值。
        snapshot = _MarketLeaderboardCache(
            categories=_build_market_leaderboards(),
            refreshed_at=refreshed_at,
            expires_at=expires_at,
            expires_at_monotonic=now_monotonic + MARKET_LEADERBOARD_TTL_SECONDS,
        )
        # 变量说明：_market_leaderboard_cache 表示当前步骤使用的 _market_leaderboard_cache 值。
        _market_leaderboard_cache = snapshot
        return snapshot, False


# 函数职责：完成 market_leaderboards 对应的业务处理。
# 参数关系：refresh 表示当前步骤使用的 refresh 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def market_leaderboards(*, refresh: bool = False) -> dict[str, Any]:
    """Return the five fixed, popularity-ranked marketplace categories.

    This deliberately exposes only the same inert metadata as browse/search;
    it never returns marketplace file contents, install commands, or the
    bearer/OIDC credential used for the upstream request.
    """

    # 变量说明：available 表示当前步骤使用的 available 值；message 表示当前消息。
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

    # 变量说明：snapshot 表示当前步骤使用的 snapshot 值；cached 表示当前步骤使用的 cached 值。
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


# 函数职责：完成 safe_relative_path 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _safe_relative_path(value: str) -> PurePosixPath:
    # 变量说明：path 表示当前文件或目录路径。
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


# 函数职责：完成 write_market_files 对应的业务处理。
# 参数关系：files 表示当前流程使用的 files 集合；root 表示处理范围的根目录。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _write_market_files(files: Sequence[dict[str, Any]], root: Path) -> None:
    if len(files) > MAX_SKILL_FILES:
        raise HTTPException(status_code=413, detail="Skill package has too many files")
    # 变量说明：total 表示当前步骤使用的 total 值。
    total = 0
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not isinstance(row.get("contents"), str):
            raise HTTPException(status_code=502, detail="skills.sh detail contains an invalid file entry")
        # 变量说明：relative 表示当前步骤使用的 relative 值。
        relative = _safe_relative_path(row["path"])
        # 变量说明：contents 表示当前流程使用的 contents 集合。
        contents = row["contents"].encode("utf-8")
        if len(contents) > MAX_SKILL_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"Skill file is too large: {relative.as_posix()}")
        total += len(contents)
        if total > MAX_SKILL_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="Skill package is too large")
        # 变量说明：target 表示当前步骤使用的 target 值。
        target = root.joinpath(*relative.parts)
        if not _is_within(target.resolve(strict=False), root.resolve()):
            raise HTTPException(status_code=422, detail="Skill contains an unsafe destination path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)


# 函数职责：校验 market_id 对应的数据或流程。
# 参数关系：market_id 表示market 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _validate_market_id(market_id: str) -> str:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = market_id.strip().strip("/")
    # 变量说明：parts 表示当前流程使用的 parts 集合。
    parts = normalized.split("/")
    if len(parts) < 2 or any(not _SAFE_GITHUB_PART_RE.fullmatch(part) for part in parts):
        raise HTTPException(status_code=422, detail="market_id must be a safe skills.sh identifier")
    return normalized


# 函数职责：完成 preview_market_skill 对应的业务处理。
# 参数关系：market_id 表示market 对象的唯一标识；confirm 表示当前步骤使用的 confirm 值；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def preview_market_skill(market_id: str, *, confirm: bool, db: Session) -> tuple[SkillPreview, Skill | None]:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = _validate_market_id(market_id)
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload = _skills_sh_json(f"/api/v1/skills/{quote(normalized, safe='/')}")
    # 变量说明：files 表示当前流程使用的 files 集合。
    files = payload.get("files")
    if not isinstance(files, list):
        raise HTTPException(status_code=502, detail="skills.sh does not expose a file snapshot for this Skill")
    # 变量说明：preview_files 表示当前流程使用的 preview_files 集合。
    preview_files: list[tuple[str, int]] = []
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not isinstance(row.get("contents"), str):
            raise HTTPException(status_code=502, detail="skills.sh detail contains an invalid file entry")
        # 变量说明：relative 表示当前步骤使用的 relative 值。
        relative = _safe_relative_path(row["path"])
        preview_files.append((relative.as_posix(), len(row["contents"].encode("utf-8"))))
    # 变量说明：source_url 表示source 的访问地址。
    source_url = f"https://skills.sh/{normalized}"
    # 变量说明：preview 表示当前步骤使用的 preview 值。
    preview = SkillPreview(source_url=source_url, candidates=(".",), files=tuple(preview_files), selected_root=None)
    if not confirm:
        return preview, None
    with tempfile.TemporaryDirectory(prefix="pgagent-market-") as temp_dir:
        # 变量说明：source_root 表示当前步骤使用的 source_root 值。
        source_root = Path(temp_dir)
        _write_market_files(files, source_root)
        # 变量说明：installed 表示当前步骤使用的 installed 值。
        installed = install_local_skill(
            db,
            source_root,
            source="skills_sh",
            source_url=source_url,
            preferred_slug=str(payload.get("slug") or normalized.rsplit("/", 1)[-1]),
        )
    return preview, installed


# 函数职责：校验 github_url 对应的数据或流程。
# 参数关系：url 表示当前步骤使用的 url 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _validate_github_url(url: str) -> tuple[str, list[str]]:
    # 变量说明：parsed 表示当前步骤使用的 parsed 值。
    parsed = urlparse(url.strip())
    # 变量说明：host 表示当前步骤使用的 host 值。
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in GITHUB_ALLOWED_HOSTS or parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Only public HTTPS GitHub repository or ZIP URLs are supported")
    # 变量说明：parts 表示当前流程使用的 parts 集合。
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail="GitHub source URL has an unsafe path")
    return host, parts


# 函数职责：完成 github_default_archive_url 对应的业务处理。
# 参数关系：source_url 表示source 的访问地址。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _github_default_archive_url(source_url: str) -> str:
    # 变量说明：host 表示当前步骤使用的 host 值；parts 表示当前流程使用的 parts 集合。
    host, parts = _validate_github_url(source_url)
    if host != "github.com" or len(parts) != 2 or not all(_SAFE_GITHUB_PART_RE.fullmatch(part) for part in parts):
        raise HTTPException(status_code=422, detail="Repository URL must be https://github.com/{owner}/{repo}")
    # 变量说明：owner 表示当前步骤使用的 owner 值；repo 表示当前步骤使用的 repo 值。
    owner, repo = parts
    try:
        # 变量说明：response 表示下游返回的响应。
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
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="GitHub repository lookup returned invalid JSON") from exc
    # 变量说明：branch 表示当前步骤使用的 branch 值。
    branch = payload.get("default_branch") if isinstance(payload, dict) else None
    if not isinstance(branch, str) or not branch or not _SAFE_GITHUB_PART_RE.fullmatch(branch):
        raise HTTPException(status_code=502, detail="GitHub repository has no safe default branch")
    return f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/zipball/{quote(branch)}"


# 函数职责：完成 archive_url_for_source 对应的业务处理。
# 参数关系：source_url 表示source 的访问地址。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _archive_url_for_source(source_url: str) -> str:
    # 变量说明：host 表示当前步骤使用的 host 值；parts 表示当前流程使用的 parts 集合。
    host, parts = _validate_github_url(source_url)
    if host == "github.com" and len(parts) == 2:
        return _github_default_archive_url(source_url)
    # 变量说明：path 表示当前文件或目录路径。
    path = urlparse(source_url).path.lower()
    # 变量说明：is_codeload_zip 表示表示是否满足 codeload_zip 条件的布尔标记。
    is_codeload_zip = host == "codeload.github.com" and len(parts) >= 4 and parts[2] == "zip"
    if not path.endswith(".zip") and not is_codeload_zip:
        raise HTTPException(status_code=422, detail="GitHub archive URL must end in .zip")
    return source_url


# 函数职责：完成 download_github_archive 对应的业务处理。
# 参数关系：url 表示当前步骤使用的 url 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _download_github_archive(url: str) -> bytes:
    _validate_github_url(url)
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"Accept": "application/zip"}) as client:
            with client.stream("GET", url) as response:
                # 变量说明：history 表示当前步骤使用的 history 值。
                history = [*response.history, response]
                for hop in history:
                    # 变量说明：parsed 表示当前步骤使用的 parsed 值。
                    parsed = urlparse(str(hop.url))
                    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in GITHUB_ALLOWED_HOSTS:
                        raise HTTPException(status_code=422, detail="GitHub archive redirected to an untrusted host")
                if response.status_code == 404:
                    raise HTTPException(status_code=404, detail="GitHub archive was not found")
                if response.status_code >= 400:
                    raise HTTPException(status_code=502, detail=f"GitHub archive returned HTTP {response.status_code}")
                # 变量说明：chunks 表示当前流程使用的 chunks 集合。
                chunks: list[bytes] = []
                # 变量说明：size 表示当前步骤使用的 size 值。
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


# 函数职责：完成 extract_safe_zip 对应的业务处理。
# 参数关系：archive 表示当前步骤使用的 archive 值；root 表示处理范围的根目录。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _extract_safe_zip(archive: bytes, root: Path) -> None:
    try:
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            # 变量说明：infos 表示当前流程使用的 infos 集合。
            infos = [item for item in bundle.infolist() if not item.is_dir()]
            if len(infos) > MAX_ARCHIVE_FILES:
                raise HTTPException(status_code=413, detail="GitHub archive has too many files")
            # 变量说明：total 表示当前步骤使用的 total 值。
            total = 0
            for info in infos:
                # 变量说明：relative 表示当前步骤使用的 relative 值。
                relative = _safe_relative_path(info.filename)
                # 变量说明：unix_mode 表示当前步骤使用的 unix_mode 值。
                unix_mode = (info.external_attr >> 16) & 0o170000
                if unix_mode == 0o120000:
                    raise HTTPException(status_code=422, detail=f"GitHub archive contains a symlink: {relative.as_posix()}")
                if info.file_size > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise HTTPException(status_code=413, detail=f"GitHub archive file is too large: {relative.as_posix()}")
                total += info.file_size
                if total > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise HTTPException(status_code=413, detail="GitHub archive expands beyond the safe inspection limit")
                # 变量说明：target 表示当前步骤使用的 target 值。
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


# 函数职责：查找 skill_directories 对应的数据或流程。
# 参数关系：root 表示处理范围的根目录。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _find_skill_directories(root: Path) -> list[Path]:
    return sorted(
        {path.parent.resolve() for path in root.rglob("SKILL.md") if path.is_file() and not path.is_symlink()},
        key=lambda path: path.as_posix(),
    )


# 函数职责：完成 relative_candidate 对应的业务处理。
# 参数关系：root 表示处理范围的根目录；candidate 表示当前步骤使用的 candidate 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _relative_candidate(root: Path, candidate: Path) -> str:
    return candidate.relative_to(root).as_posix() or "."


# 函数职责：完成 select_skill_directory 对应的业务处理。
# 参数关系：root 表示处理范围的根目录；skill_path 表示skill_path 对应的文件系统位置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _select_skill_directory(root: Path, skill_path: str | None) -> tuple[list[Path], Path | None]:
    # 变量说明：candidates 表示当前流程使用的 candidates 集合。
    candidates = _find_skill_directories(root)
    if not candidates:
        raise HTTPException(status_code=422, detail="GitHub archive contains no SKILL.md folder")
    if skill_path:
        # 变量说明：relative 表示当前步骤使用的 relative 值。
        relative = _safe_relative_path(skill_path)
        # 变量说明：selected 表示当前步骤使用的 selected 值。
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


# 函数职责：完成 preview_github_skill 对应的业务处理。
# 参数关系：source_url 表示source 的访问地址；skill_path 表示skill_path 对应的文件系统位置；confirm 表示当前步骤使用的 confirm 值；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def preview_github_skill(
    source_url: str,
    *,
    skill_path: str | None,
    confirm: bool,
    db: Session,
) -> tuple[SkillPreview, Skill | None]:
    # 变量说明：archive_url 表示archive 的访问地址。
    archive_url = _archive_url_for_source(source_url)
    # 变量说明：archive 表示当前步骤使用的 archive 值。
    archive = _download_github_archive(archive_url)
    with tempfile.TemporaryDirectory(prefix="pgagent-github-") as temp_dir:
        # 变量说明：extraction_root 表示当前步骤使用的 extraction_root 值。
        extraction_root = Path(temp_dir)
        _extract_safe_zip(archive, extraction_root)
        # 变量说明：candidates 表示当前流程使用的 candidates 集合；selected 表示当前步骤使用的 selected 值。
        candidates, selected = _select_skill_directory(extraction_root, skill_path)
        # 变量说明：preview 表示当前步骤使用的 preview 值。
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
        # 变量说明：installed 表示当前步骤使用的 installed 值。
        installed = install_local_skill(db, selected, source="github", source_url=source_url)
        return preview, installed
