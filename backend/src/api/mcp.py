"""MCP configuration management and live session status API."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.config import settings
from src.mcp.config import (
    McpConfig,
    McpServerConfig,
    load_mcp_config,
    load_mcp_config_source,
    save_mcp_config,
)
from src.mcp.runtime import mcp_runtime_pool
from src.persistence.database import Run, Session as ChatSession, get_db


router = APIRouter(prefix="/api/mcp", tags=["mcp"])
_config_write_lock = asyncio.Lock()
_CONFIG_BLOCKING_RUN_STATUSES = frozenset({
    "received", "preparing_context", "planning", "acting", "observing", "verifying", "running", "awaiting_approval",
})
_CONFIG_BLOCKING_STOP_REASONS = frozenset({
    "waiting_background", "delegated_child_awaiting_approval", "delegated_child_waiting_event",
})


class McpServerWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    command: str = Field(min_length=1, max_length=2000)
    args: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True

    @field_validator("name", "command")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class McpServerRead(BaseModel):
    name: str
    command: str
    args: list[str]
    enabled: bool
    transport: str = "stdio"
    runtime_status: str = "inactive"
    tool_count: int = 0


class McpServerToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _editable_server(name: str, server: McpServerConfig) -> McpServerRead:
    runtime_status = "inactive"
    tool_count = 0
    status_priority = {"inactive": 0, "dormant": 1, "ready": 2, "degraded": 3, "failed": 4}
    for session in mcp_runtime_pool.statuses():
        for current in session["servers"]:
            if current["name"] == name:
                candidate = str(current["status"])
                if status_priority.get(candidate, 0) > status_priority.get(runtime_status, 0):
                    runtime_status = candidate
                tool_count = max(tool_count, int(current["tool_count"]))
    return McpServerRead(
        name=name,
        command=server.command or "",
        args=server.args,
        enabled=server.enabled,
        transport=server.transport,
        runtime_status=runtime_status,
        tool_count=tool_count,
    )


def _source_config() -> McpConfig:
    try:
        return load_mcp_config_source(settings.mcp_config_file)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="MCP 配置文件格式无效") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="无法读取 MCP 配置文件") from exc


def _require_no_active_run(db: Session) -> None:
    active_run_id = db.scalar(select(Run.id).where(or_(
        Run.status.in_(_CONFIG_BLOCKING_RUN_STATUSES),
        Run.stop_reason.in_(_CONFIG_BLOCKING_STOP_REASONS),
    )).limit(1))
    if active_run_id is not None:
        raise HTTPException(status_code=409, detail="有 Agent 运行尚未结束，请在运行结束后再修改 MCP 配置")


def _persist(config: McpConfig) -> None:
    try:
        save_mcp_config(settings.mcp_config_file, config)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法保存 MCP 配置文件") from exc


def _replace_session_selection(
    db: Session,
    server_name: str,
    replacement: str | None,
) -> None:
    for chat_session in db.scalars(select(ChatSession)):
        selected = list(chat_session.mcp_server_names or [])
        if server_name not in selected:
            continue
        chat_session.mcp_server_names = list(dict.fromkeys(
            replacement if name == server_name and replacement else name
            for name in selected
            if name != server_name or replacement
        ))


async def _commit_session_selection(db: Session, previous_config: McpConfig) -> None:
    try:
        db.commit()
    except BaseException:
        db.rollback()
        await mcp_runtime_pool.apply_configuration(lambda: _persist(previous_config))
        raise


@router.get("")
async def mcp_status() -> dict:
    """Expose server metadata without returning commands, headers, or secrets."""

    try:
        config = load_mcp_config(settings.mcp_config_file)
    except ValidationError as exc:
        issues = [
            {"location": list(item["loc"]), "type": item["type"], "message": item["msg"]}
            for item in exc.errors(include_input=False, include_url=False)
        ]
        raise HTTPException(status_code=422, detail={"message": "Invalid MCP configuration", "issues": issues}) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": "Invalid MCP configuration", "error_code": type(exc).__name__},
        ) from exc
    return {
        "config_file": str(settings.mcp_config_file),
        "configured_servers": [
            {
                "name": name,
                "enabled": server.enabled,
                "required": server.required,
                "transport": server.transport,
            }
            for name, server in sorted(config.servers.items())
        ],
        "sessions": mcp_runtime_pool.statuses(),
    }


@router.get("/servers", response_model=list[McpServerRead])
async def list_mcp_servers() -> list[McpServerRead]:
    config = _source_config()
    return [_editable_server(name, server) for name, server in sorted(config.servers.items())]


@router.post("/servers", response_model=McpServerRead, status_code=status.HTTP_201_CREATED)
async def create_mcp_server(payload: McpServerWrite, db: Session = Depends(get_db)) -> McpServerRead:
    async with _config_write_lock:
        def change() -> McpServerConfig:
            _require_no_active_run(db)
            config = _source_config()
            if payload.name in config.servers:
                raise HTTPException(status_code=409, detail="已存在同名 MCP 服务器")
            server = McpServerConfig(command=payload.command, args=payload.args, enabled=payload.enabled)
            config.servers[payload.name] = server
            _persist(config)
            return server

        server = await mcp_runtime_pool.apply_configuration(change)
        return _editable_server(payload.name, server)


@router.put("/servers/{server_name:path}", response_model=McpServerRead)
async def update_mcp_server(
    server_name: str,
    payload: McpServerWrite,
    db: Session = Depends(get_db),
) -> McpServerRead:
    async with _config_write_lock:
        def change() -> tuple[McpServerConfig, McpConfig]:
            _require_no_active_run(db)
            config = _source_config()
            previous_config = config.model_copy(deep=True)
            existing = config.servers.get(server_name)
            if existing is None:
                raise HTTPException(status_code=404, detail="MCP 服务器不存在")
            if payload.name != server_name and payload.name in config.servers:
                raise HTTPException(status_code=409, detail="已存在同名 MCP 服务器")
            if existing.transport != "stdio":
                raise HTTPException(status_code=409, detail="流式 HTTP MCP 请继续通过高级配置文件管理")
            server = existing.model_copy(update={
                "command": payload.command,
                "args": payload.args,
                "enabled": payload.enabled,
            })
            if payload.name != server_name:
                items = [
                    (payload.name if name == server_name else name, server if name == server_name else item)
                    for name, item in config.servers.items()
                ]
                config.servers = dict(items)
            else:
                config.servers[server_name] = server
            _persist(config)
            return server, previous_config

        server, previous_config = await mcp_runtime_pool.apply_configuration(change)
        if payload.name != server_name:
            _replace_session_selection(db, server_name, payload.name if payload.enabled else None)
        elif not payload.enabled:
            _replace_session_selection(db, server_name, None)
        await _commit_session_selection(db, previous_config)
        return _editable_server(payload.name, server)


@router.patch("/servers/{server_name:path}", response_model=McpServerRead)
async def toggle_mcp_server(
    server_name: str,
    payload: McpServerToggle,
    db: Session = Depends(get_db),
) -> McpServerRead:
    async with _config_write_lock:
        def change() -> tuple[McpServerConfig, McpConfig]:
            _require_no_active_run(db)
            config = _source_config()
            previous_config = config.model_copy(deep=True)
            existing = config.servers.get(server_name)
            if existing is None:
                raise HTTPException(status_code=404, detail="MCP 服务器不存在")
            server = existing.model_copy(update={"enabled": payload.enabled})
            config.servers[server_name] = server
            _persist(config)
            return server, previous_config

        server, previous_config = await mcp_runtime_pool.apply_configuration(change)
        if not payload.enabled:
            _replace_session_selection(db, server_name, None)
        await _commit_session_selection(db, previous_config)
        return _editable_server(server_name, server)


@router.delete("/servers/{server_name:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(server_name: str, db: Session = Depends(get_db)) -> Response:
    async with _config_write_lock:
        def change() -> McpConfig:
            _require_no_active_run(db)
            config = _source_config()
            previous_config = config.model_copy(deep=True)
            if server_name not in config.servers:
                raise HTTPException(status_code=404, detail="MCP 服务器不存在")
            del config.servers[server_name]
            _persist(config)
            return previous_config

        previous_config = await mcp_runtime_pool.apply_configuration(change)
        _replace_session_selection(db, server_name, None)
        await _commit_session_selection(db, previous_config)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
