"""Read and update the process-wide permission preference."""

from __future__ import annotations

from typing import Literal

from sqlalchemy.orm import Session

from src.persistence.models import PermissionSettings


GLOBAL_PERMISSION_SETTINGS_ID = "global"
PermissionMode = Literal["ask", "smart", "full"]


def get_permission_settings(db: Session) -> PermissionSettings:
    """Return the seeded global permission preference row."""

    settings = db.get(PermissionSettings, GLOBAL_PERMISSION_SETTINGS_ID)
    if settings is None:
        raise RuntimeError("Global permission settings have not been initialized")
    return settings


def get_permission_mode(db: Session) -> str:
    return str(get_permission_settings(db).permission_mode or "smart")


def set_permission_mode(db: Session, mode: PermissionMode) -> PermissionSettings:
    """Persist the user's latest choice for subsequent new sessions."""

    settings = get_permission_settings(db)
    settings.permission_mode = mode
    return settings
