"""MCP configuration management and live session status API."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 mcp 子模块。
# 逻辑关系：上层通过 api/mcp.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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


# 变量说明：router 表示当前步骤使用的 router 值。
router = APIRouter(prefix="/api/mcp", tags=["mcp"])
# 变量说明：_config_write_lock 表示当前步骤使用的 _config_write_lock 值。
_config_write_lock = asyncio.Lock()
# 变量说明：_CONFIG_BLOCKING_RUN_STATUSES 表示当前流程使用的 _CONFIG_BLOCKING_RUN_STATUSES 集合。
_CONFIG_BLOCKING_RUN_STATUSES = frozenset({
    "received", "preparing_context", "planning", "acting", "observing", "verifying", "running", "awaiting_approval",
})
# 变量说明：_CONFIG_BLOCKING_STOP_REASONS 表示当前流程使用的 _CONFIG_BLOCKING_STOP_REASONS 集合。
_CONFIG_BLOCKING_STOP_REASONS = frozenset({
    "waiting_background", "delegated_child_awaiting_approval", "delegated_child_waiting_event",
})


# 类职责：定义 McpServerWrite 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class McpServerWrite(BaseModel):
    # 变量说明：model_config 表示当前步骤使用的 model_config 值。
    model_config = ConfigDict(extra="forbid")

    # 变量说明：name 表示当前对象名称。
    name: str = Field(min_length=1, max_length=100)
    # 变量说明：command 表示当前步骤使用的 command 值。
    command: str = Field(min_length=1, max_length=2000)
    # 变量说明：args 表示当前流程使用的 args 集合。
    args: list[str] = Field(default_factory=list, max_length=100)
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool = True

    # 函数职责：完成 strip_required_text 对应的业务处理。
    # 参数关系：value 表示当前字段或计算值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @field_validator("name", "command")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


# 类职责：定义 McpServerRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class McpServerRead(BaseModel):
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：command 表示当前步骤使用的 command 值。
    command: str
    # 变量说明：args 表示当前流程使用的 args 集合。
    args: list[str]
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool
    # 变量说明：transport 表示当前步骤使用的 transport 值。
    transport: str = "stdio"
    # 变量说明：runtime_status 表示当前流程使用的 runtime_status 集合。
    runtime_status: str = "inactive"
    # 变量说明：tool_count 表示tool 的数量。
    tool_count: int = 0


# 类职责：定义 McpServerToggle 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class McpServerToggle(BaseModel):
    # 变量说明：model_config 表示当前步骤使用的 model_config 值。
    model_config = ConfigDict(extra="forbid")

    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool


# 函数职责：完成 editable_server 对应的业务处理。
# 参数关系：name 表示当前对象名称；server 表示当前步骤使用的 server 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _editable_server(name: str, server: McpServerConfig) -> McpServerRead:
    # 变量说明：runtime_status 表示当前流程使用的 runtime_status 集合。
    runtime_status = "inactive"
    # 变量说明：tool_count 表示tool 的数量。
    tool_count = 0
    # 变量说明：status_priority 表示当前步骤使用的 status_priority 值。
    status_priority = {"inactive": 0, "dormant": 1, "ready": 2, "degraded": 3, "failed": 4}
    for session in mcp_runtime_pool.statuses():
        for current in session["servers"]:
            if current["name"] == name:
                # 变量说明：candidate 表示当前步骤使用的 candidate 值。
                candidate = str(current["status"])
                if status_priority.get(candidate, 0) > status_priority.get(runtime_status, 0):
                    # 变量说明：runtime_status 表示当前流程使用的 runtime_status 集合。
                    runtime_status = candidate
                # 变量说明：tool_count 表示tool 的数量。
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


# 函数职责：完成 source_config 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _source_config() -> McpConfig:
    try:
        return load_mcp_config_source(settings.mcp_config_file)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="MCP 配置文件格式无效") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="无法读取 MCP 配置文件") from exc


# 函数职责：完成 require_no_active_run 对应的业务处理。
# 参数关系：db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _require_no_active_run(db: Session) -> None:
    # 变量说明：active_run_id 表示active_run 对象的唯一标识。
    active_run_id = db.scalar(select(Run.id).where(or_(
        Run.status.in_(_CONFIG_BLOCKING_RUN_STATUSES),
        Run.stop_reason.in_(_CONFIG_BLOCKING_STOP_REASONS),
    )).limit(1))
    if active_run_id is not None:
        raise HTTPException(status_code=409, detail="有 Agent 运行尚未结束，请在运行结束后再修改 MCP 配置")


# 函数职责：完成 persist 对应的业务处理。
# 参数关系：config 表示当前生效的配置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _persist(config: McpConfig) -> None:
    try:
        save_mcp_config(settings.mcp_config_file, config)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法保存 MCP 配置文件") from exc


# 函数职责：完成 replace_session_selection 对应的业务处理。
# 参数关系：db 表示当前数据库会话；server_name 表示当前步骤使用的 server_name 值；replacement 表示当前步骤使用的 replacement 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _replace_session_selection(
    db: Session,
    server_name: str,
    replacement: str | None,
) -> None:
    for chat_session in db.scalars(select(ChatSession)):
        # 变量说明：selected 表示当前步骤使用的 selected 值。
        selected = list(chat_session.mcp_server_names or [])
        if server_name not in selected:
            continue
        # 变量说明：mcp_server_names 表示当前流程使用的 mcp_server_names 集合。
        chat_session.mcp_server_names = list(dict.fromkeys(
            replacement if name == server_name and replacement else name
            for name in selected
            if name != server_name or replacement
        ))


# 函数职责：异步完成 commit_session_selection 对应的业务处理。
# 参数关系：db 表示当前数据库会话；previous_config 表示当前步骤使用的 previous_config 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def _commit_session_selection(db: Session, previous_config: McpConfig) -> None:
    try:
        db.commit()
    except BaseException:
        db.rollback()
        await mcp_runtime_pool.apply_configuration(lambda: _persist(previous_config))
        raise


# 函数职责：异步完成 mcp_status 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("")
async def mcp_status() -> dict:
    """Expose server metadata without returning commands, headers, or secrets."""

    try:
        # 变量说明：config 表示当前生效的配置。
        config = load_mcp_config(settings.mcp_config_file)
    except ValidationError as exc:
        # 变量说明：issues 表示当前流程使用的 issues 集合。
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


# 函数职责：异步列出 mcp_servers 对应的数据或流程。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/servers", response_model=list[McpServerRead])
async def list_mcp_servers() -> list[McpServerRead]:
    # 变量说明：config 表示当前生效的配置。
    config = _source_config()
    return [_editable_server(name, server) for name, server in sorted(config.servers.items())]


# 函数职责：异步创建 mcp_server 对应的数据或流程。
# 参数关系：payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/servers", response_model=McpServerRead, status_code=status.HTTP_201_CREATED)
async def create_mcp_server(payload: McpServerWrite, db: Session = Depends(get_db)) -> McpServerRead:
    async with _config_write_lock:
        # 函数职责：完成 change 对应的业务处理。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def change() -> McpServerConfig:
            _require_no_active_run(db)
            # 变量说明：config 表示当前生效的配置。
            config = _source_config()
            if payload.name in config.servers:
                raise HTTPException(status_code=409, detail="已存在同名 MCP 服务器")
            # 变量说明：server 表示当前步骤使用的 server 值。
            server = McpServerConfig(command=payload.command, args=payload.args, enabled=payload.enabled)
            config.servers[payload.name] = server
            _persist(config)
            return server

        # 变量说明：server 表示当前步骤使用的 server 值。
        server = await mcp_runtime_pool.apply_configuration(change)
        return _editable_server(payload.name, server)


# 函数职责：异步更新 mcp_server 对应的数据或流程。
# 参数关系：server_name 表示当前步骤使用的 server_name 值；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.put("/servers/{server_name:path}", response_model=McpServerRead)
async def update_mcp_server(
    server_name: str,
    payload: McpServerWrite,
    db: Session = Depends(get_db),
) -> McpServerRead:
    async with _config_write_lock:
        # 函数职责：完成 change 对应的业务处理。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def change() -> tuple[McpServerConfig, McpConfig]:
            _require_no_active_run(db)
            # 变量说明：config 表示当前生效的配置。
            config = _source_config()
            # 变量说明：previous_config 表示当前步骤使用的 previous_config 值。
            previous_config = config.model_copy(deep=True)
            # 变量说明：existing 表示当前步骤使用的 existing 值。
            existing = config.servers.get(server_name)
            if existing is None:
                raise HTTPException(status_code=404, detail="MCP 服务器不存在")
            if payload.name != server_name and payload.name in config.servers:
                raise HTTPException(status_code=409, detail="已存在同名 MCP 服务器")
            if existing.transport != "stdio":
                raise HTTPException(status_code=409, detail="流式 HTTP MCP 请继续通过高级配置文件管理")
            # 变量说明：server 表示当前步骤使用的 server 值。
            server = existing.model_copy(update={
                "command": payload.command,
                "args": payload.args,
                "enabled": payload.enabled,
            })
            if payload.name != server_name:
                # 变量说明：items 表示待处理的元素集合。
                items = [
                    (payload.name if name == server_name else name, server if name == server_name else item)
                    for name, item in config.servers.items()
                ]
                # 变量说明：servers 表示当前流程使用的 servers 集合。
                config.servers = dict(items)
            else:
                config.servers[server_name] = server
            _persist(config)
            return server, previous_config

        # 变量说明：server 表示当前步骤使用的 server 值；previous_config 表示当前步骤使用的 previous_config 值。
        server, previous_config = await mcp_runtime_pool.apply_configuration(change)
        if payload.name != server_name:
            _replace_session_selection(db, server_name, payload.name if payload.enabled else None)
        elif not payload.enabled:
            _replace_session_selection(db, server_name, None)
        await _commit_session_selection(db, previous_config)
        return _editable_server(payload.name, server)


# 函数职责：异步完成 toggle_mcp_server 对应的业务处理。
# 参数关系：server_name 表示当前步骤使用的 server_name 值；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.patch("/servers/{server_name:path}", response_model=McpServerRead)
async def toggle_mcp_server(
    server_name: str,
    payload: McpServerToggle,
    db: Session = Depends(get_db),
) -> McpServerRead:
    async with _config_write_lock:
        # 函数职责：完成 change 对应的业务处理。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def change() -> tuple[McpServerConfig, McpConfig]:
            _require_no_active_run(db)
            # 变量说明：config 表示当前生效的配置。
            config = _source_config()
            # 变量说明：previous_config 表示当前步骤使用的 previous_config 值。
            previous_config = config.model_copy(deep=True)
            # 变量说明：existing 表示当前步骤使用的 existing 值。
            existing = config.servers.get(server_name)
            if existing is None:
                raise HTTPException(status_code=404, detail="MCP 服务器不存在")
            # 变量说明：server 表示当前步骤使用的 server 值。
            server = existing.model_copy(update={"enabled": payload.enabled})
            config.servers[server_name] = server
            _persist(config)
            return server, previous_config

        # 变量说明：server 表示当前步骤使用的 server 值；previous_config 表示当前步骤使用的 previous_config 值。
        server, previous_config = await mcp_runtime_pool.apply_configuration(change)
        if not payload.enabled:
            _replace_session_selection(db, server_name, None)
        await _commit_session_selection(db, previous_config)
        return _editable_server(server_name, server)


# 函数职责：异步删除 mcp_server 对应的数据或流程。
# 参数关系：server_name 表示当前步骤使用的 server_name 值；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.delete("/servers/{server_name:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(server_name: str, db: Session = Depends(get_db)) -> Response:
    async with _config_write_lock:
        # 函数职责：完成 change 对应的业务处理。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def change() -> McpConfig:
            _require_no_active_run(db)
            # 变量说明：config 表示当前生效的配置。
            config = _source_config()
            # 变量说明：previous_config 表示当前步骤使用的 previous_config 值。
            previous_config = config.model_copy(deep=True)
            if server_name not in config.servers:
                raise HTTPException(status_code=404, detail="MCP 服务器不存在")
            del config.servers[server_name]
            _persist(config)
            return previous_config

        # 变量说明：previous_config 表示当前步骤使用的 previous_config 值。
        previous_config = await mcp_runtime_pool.apply_configuration(change)
        _replace_session_selection(db, server_name, None)
        await _commit_session_selection(db, previous_config)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
