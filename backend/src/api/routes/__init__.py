"""Domain-oriented HTTP resource routers.

Each module owns one public API area.  Re-exports preserve the original
Python import surface for callers and tests while the HTTP wiring remains
explicit below.
"""

from fastapi import APIRouter

from src.config import settings
from src.api.routes import agents as agent_routes
from src.api.routes import approvals as approval_routes
from src.api.routes import dashboard as dashboard_routes
from src.api.routes import memories as memory_routes
from src.api.routes import runs as run_routes
from src.api.routes import sessions as session_routes
from src.api.routes import workspaces as workspace_routes
from src.api.routes.agents import create_agent, delete_agent, get_agent, list_agents, update_agent
from src.api.routes.approvals import list_approvals
from src.api.routes.dashboard import dashboard
from src.api.routes.memories import (
    clear_memories,
    create_memory,
    delete_memory,
    list_memories,
    list_memory_recalls,
    search_memories,
    update_memory,
)
from src.api.routes.runs import create_run, create_run_event, get_run, list_run_events, list_runs, update_run
from src.api.routes.sessions import (
    create_message,
    create_session,
    delete_session,
    get_session,
    get_session_active_task,
    list_messages,
    list_session_background_jobs,
    list_session_collaboration_events,
    list_session_collaboration_messages,
    list_session_delegations,
    list_session_tasks,
    list_session_teammates,
    list_sessions,
    update_session,
)
from src.api.routes.workspaces import create_workspace, delete_workspace, get_workspace, list_workspaces, update_workspace


router = APIRouter()
router.include_router(dashboard_routes.router)
router.include_router(workspace_routes.router)
router.include_router(agent_routes.router)
router.include_router(session_routes.router)
router.include_router(run_routes.router)
router.include_router(approval_routes.router)
router.include_router(memory_routes.router)
