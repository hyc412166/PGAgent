"""Persistent-memory preferences shared by API and runtime layers."""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.persistence.models import MemorySettings


GLOBAL_MEMORY_SETTINGS_ID = "global"


def get_memory_settings(db: Session) -> MemorySettings:
    """Return the seeded process-wide memory settings row."""

    settings = db.get(MemorySettings, GLOBAL_MEMORY_SETTINGS_ID)
    if settings is None:
        raise RuntimeError("Global memory settings have not been initialized")
    return settings


def memories_enabled(db: Session) -> bool:
    return bool(get_memory_settings(db).enabled)
