"""PGAgent FastAPI application and local SPA host."""

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
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api import capabilities, connections, resources, runtime, system, usage
from app.config import PROJECT_ROOT, settings
from app.database import init_db
from app.services.run_service import coordinator


logger = logging.getLogger(__name__)


async def _delivery_watchdog() -> None:
    """Repair rare post-acceptance gaps without involving another model."""

    while True:
        await asyncio.sleep(10)
        try:
            coordinator.reconcile_orphaned_runs()
            coordinator.reconcile_terminal_deliveries(include_legacy=False)
        except Exception:
            # A temporary database failure must not permanently disable later
            # repair attempts. The exception remains available in local logs.
            logger.exception("PGAgent delivery watchdog reconciliation failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    continuation_run_ids = coordinator.reconcile_interrupted_runs()
    coordinator.reconcile_terminal_deliveries()
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
        await saver.setup()
        coordinator.set_checkpointer(saver)
        for run_id in continuation_run_ids:
            coordinator.launch_delegated_child_continuation(run_id)
        watchdog = asyncio.create_task(_delivery_watchdog(), name="pgagent-delivery-watchdog")
        try:
            yield
        finally:
            watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await watchdog
            await coordinator.shutdown()
            coordinator.set_checkpointer(None)


_LOCAL_BROWSER_HOSTS = frozenset({"127.0.0.1", "localhost"})
_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _is_local_browser_origin(origin: str) -> bool:
    try:
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


@app.middleware("http")
async def enforce_local_browser_origin(request: Request, call_next):  # type: ignore[no-untyped-def]
    origin = request.headers.get("origin")
    if request.method.upper() not in _SAFE_HTTP_METHODS and origin and not _is_local_browser_origin(origin):
        return JSONResponse(status_code=403, content={"detail": "State-changing browser requests require a local origin"})
    return await call_next(request)


app.include_router(resources.router)
app.include_router(capabilities.router)
app.include_router(connections.router)
app.include_router(runtime.router)
app.include_router(system.router)
app.include_router(usage.router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "name": "PGAgent", "version": __version__}


FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str) -> FileResponse:
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")
    if not FRONTEND_DIST.exists():
        raise HTTPException(status_code=503, detail="前端尚未构建，请先运行 npm run build")
    requested = (FRONTEND_DIST / path).resolve()
    if path and requested.is_file() and FRONTEND_DIST.resolve() in requested.parents:
        return FileResponse(requested)
    return FileResponse(FRONTEND_DIST / "index.html")
