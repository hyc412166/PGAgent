"""PGAgent FastAPI application and local SPA host."""
# 文件职责：负责后端应用启动、路由注册与生命周期装配。
# 逻辑关系：本模块接收上层运行服务的输入，推进智能体执行后把状态、事件或结果返回调用层。

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src import __version__
from src.api import capabilities, connections, mcp, routes, runtime, system, usage
from src.attachments.api import router as attachments_router
from src.config import PROJECT_ROOT, settings
from src.persistence.database import init_db
from src.observability import configure_observability_logging
from src.runs.service import coordinator
from src.tasks.background import background_job_manager
from src.memory.service import import_workspace_memory_files, refresh_memory_markdown_projection
from src.memory.pipeline import activate_deferred_memory_jobs


# 变量说明：logger 表示日志记录器。
logger = logging.getLogger(__name__)


# 函数职责：异步完成 delivery_watchdog 对应的智能体处理。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
async def _delivery_watchdog() -> None:
    """Repair rare post-acceptance gaps without involving another model."""

    while True:
        await asyncio.sleep(10)
        try:
            coordinator.reconcile_orphaned_runs()
            coordinator.reconcile_terminal_deliveries(include_legacy=False)
            coordinator.reconcile_waiting_background_runs()
            activate_deferred_memory_jobs()
            for job_id in coordinator.pending_memory_job_ids(recover_running=True):
                coordinator.launch_memory_job(job_id)
        except Exception:
            # A temporary database failure must not permanently disable later
            # repair attempts. The exception remains available in local logs.
            logger.exception("PGAgent delivery watchdog reconciliation failed")


# 函数职责：异步完成 lifespan 对应的智能体处理。
# 参数关系：_app 表示当前步骤使用的 _app 值。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_observability_logging(
        level=settings.log_level,
        log_dir=settings.resolved_log_dir,
        retention_days=settings.log_retention_days,
        max_bytes=settings.log_max_bytes,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    import_workspace_memory_files()
    refresh_memory_markdown_projection()
    activate_deferred_memory_jobs()
    # 变量说明：continuation_run_ids 表示continuation_run_ids 集合。
    continuation_run_ids = coordinator.reconcile_interrupted_runs()
    # 变量说明：memory_job_ids 表示memory_job_ids 集合。
    memory_job_ids = coordinator.pending_memory_job_ids()
    coordinator.reconcile_terminal_deliveries()
    coordinator.start()
    background_job_manager.set_terminal_listener(coordinator.notify_background_terminal)
    background_job_manager.recover()
    coordinator.reconcile_waiting_background_runs()
    for run_id in continuation_run_ids:
        coordinator.launch_delegated_child_continuation(run_id)
    for job_id in memory_job_ids:
        coordinator.launch_memory_job(job_id)
    # 变量说明：watchdog 表示当前步骤使用的 watchdog 值。
    watchdog = asyncio.create_task(_delivery_watchdog(), name="pgagent-delivery-watchdog")
    try:
        yield
    finally:
        watchdog.cancel()
        with suppress(asyncio.CancelledError):
            await watchdog
        background_job_manager.set_terminal_listener(None)
        background_job_manager.shutdown()
        await coordinator.shutdown()


# 变量说明：_LOCAL_BROWSER_HOSTS 表示_LOCAL_BROWSER_HOSTS 集合。
_LOCAL_BROWSER_HOSTS = frozenset({"127.0.0.1", "localhost"})
# 变量说明：_SAFE_HTTP_METHODS 表示_SAFE_HTTP_METHODS 集合。
_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


# 函数职责：判断是否 local_browser_origin 对应流程。
# 参数关系：origin 表示当前步骤使用的 origin 值。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
def _is_local_browser_origin(origin: str) -> bool:
    try:
        # 变量说明：parsed 表示当前步骤使用的 parsed 值。
        parsed = urlsplit(origin)
        # Accessing port also rejects malformed values such as ":not-a-port".
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname in _LOCAL_BROWSER_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
    )


# 变量说明：app 表示FastAPI 应用实例。
app = FastAPI(title="PGAgent", version=__version__, lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 函数职责：异步完成 enforce_local_browser_origin 对应的智能体处理。
# 参数关系：request 表示调用请求；call_next 表示当前步骤使用的 call_next 值。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
@app.middleware("http")
async def enforce_local_browser_origin(request: Request, call_next):  # type: ignore[no-untyped-def]
    # 变量说明：origin 表示当前步骤使用的 origin 值。
    origin = request.headers.get("origin")
    if request.method.upper() not in _SAFE_HTTP_METHODS and origin and not _is_local_browser_origin(origin):
        return JSONResponse(status_code=403, content={"detail": "State-changing browser requests require a local origin"})
    return await call_next(request)


app.include_router(routes.router)
app.include_router(capabilities.router)
app.include_router(connections.router)
app.include_router(mcp.router)
app.include_router(runtime.router)
app.include_router(attachments_router)
app.include_router(system.router)
app.include_router(usage.router)


# 函数职责：完成 health 对应的智能体处理。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "name": "PGAgent", "version": __version__}


# 变量说明：FRONTEND_DIST 表示当前步骤使用的 FRONTEND_DIST 值。
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


# 函数职责：完成 frontend 对应的智能体处理。
# 参数关系：path 表示当前步骤使用的 path 值。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str) -> FileResponse:
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")
    if not FRONTEND_DIST.exists():
        raise HTTPException(status_code=503, detail="前端尚未构建，请先运行 npm run build")
    # 变量说明：requested 表示当前步骤使用的 requested 值。
    requested = (FRONTEND_DIST / path).resolve()
    if path and requested.is_file() and FRONTEND_DIST.resolve() in requested.parents:
        return FileResponse(requested)
    return FileResponse(FRONTEND_DIST / "index.html")
