"""HTTP access to safe, session-scoped attachment payloads."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session as OrmSession

from src.config import settings
from src.context.assembly import FilesystemArtifactStore
from src.persistence.database import Session, get_db

from .storage import AttachmentToolStore


router = APIRouter(prefix="/api", tags=["attachments"])


@router.get("/sessions/{session_id}/attachments/{attachment_id}/content")
def attachment_content(
    session_id: str,
    attachment_id: str,
    db: OrmSession = Depends(get_db),
) -> Response:
    if db.get(Session, session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    store = AttachmentToolStore(
        session_id,
        FilesystemArtifactStore(settings.data_dir / "artifacts" / session_id),
    )
    resolved = store.content(attachment_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    row, payload = resolved
    return Response(
        content=payload,
        media_type=row.mime_type or "application/octet-stream",
        headers={"Content-Disposition": "inline"},
    )
