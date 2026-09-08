"""HTTP access to safe, session-scoped attachment payloads."""
# 文件职责：负责附件上传、元数据与文件存储中的 api 子模块。
# 逻辑关系：上层通过 attachments/api.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session as OrmSession

from src.config import settings
from src.context.assembly import FilesystemArtifactStore
from src.persistence.database import Session, get_db

from .storage import AttachmentToolStore


# 变量说明：router 表示当前步骤使用的 router 值。
router = APIRouter(prefix="/api", tags=["attachments"])


# 函数职责：完成 attachment_content 对应的业务处理。
# 参数关系：session_id 表示所属会话标识；attachment_id 表示attachment 对象的唯一标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/sessions/{session_id}/attachments/{attachment_id}/content")
def attachment_content(
    session_id: str,
    attachment_id: str,
    db: OrmSession = Depends(get_db),
) -> Response:
    if db.get(Session, session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    # 变量说明：store 表示当前步骤使用的 store 值。
    store = AttachmentToolStore(
        session_id,
        FilesystemArtifactStore(settings.data_dir / "artifacts" / session_id),
    )
    # 变量说明：resolved 表示当前步骤使用的 resolved 值。
    resolved = store.content(attachment_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    # 变量说明：row 表示当前步骤使用的 row 值；payload 表示跨层传递的数据载荷。
    row, payload = resolved
    return Response(
        content=payload,
        media_type=row.mime_type or "application/octet-stream",
        headers={"Content-Disposition": "inline"},
    )
