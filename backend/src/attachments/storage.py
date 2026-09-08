"""Store and inspect files that belong to one conversation, not its workspace."""
# 文件职责：负责附件上传、元数据与文件存储中的 storage 子模块。
# 逻辑关系：上层通过 attachments/storage.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select

from src.context.assembly import FilesystemArtifactStore
from src.persistence import database as database_module
from src.persistence.database import Artifact
from src.tools.types import ToolResult

from .contracts import ATTACHMENT_TOOL_NAMES


# 变量说明：MAX_ATTACHMENT_COUNT 表示MAX_ATTACHMENT 的数量。
MAX_ATTACHMENT_COUNT = 10
# 变量说明：MAX_ATTACHMENT_BYTES 表示当前流程使用的 MAX_ATTACHMENT_BYTES 集合。
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
# 变量说明：MAX_ATTACHMENT_TOTAL_BYTES 表示当前流程使用的 MAX_ATTACHMENT_TOTAL_BYTES 集合。
MAX_ATTACHMENT_TOTAL_BYTES = 75 * 1024 * 1024
# 变量说明：DEFAULT_ATTACHMENT_PAGE_CHARS 表示当前流程使用的 DEFAULT_ATTACHMENT_PAGE_CHARS 集合。
DEFAULT_ATTACHMENT_PAGE_CHARS = 20_000
# 变量说明：MAX_ATTACHMENT_PAGE_CHARS 表示当前流程使用的 MAX_ATTACHMENT_PAGE_CHARS 集合。
MAX_ATTACHMENT_PAGE_CHARS = 24_000
# 变量说明：MAX_PDF_PAGES_PER_READ 表示当前步骤使用的 MAX_PDF_PAGES_PER_READ 值。
MAX_PDF_PAGES_PER_READ = 20

# 变量说明：_IMAGE_MIME_TYPES 表示当前流程使用的 _IMAGE_MIME_TYPES 集合。
_IMAGE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})
# 变量说明：_TEXT_EXTENSIONS 表示当前流程使用的 _TEXT_EXTENSIONS 集合。
_TEXT_EXTENSIONS = frozenset({
    ".c", ".cc", ".conf", ".cpp", ".cs", ".css", ".csv", ".env", ".go",
    ".h", ".hpp", ".htm", ".html", ".ini", ".java", ".js", ".json",
    ".jsx", ".log", ".md", ".mjs", ".py", ".rb", ".rs", ".sh", ".sql",
    ".svg", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
})


# 类职责：定义 UploadedAttachment 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class UploadedAttachment:
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：mime_type 表示当前步骤使用的 mime_type 值。
    mime_type: str
    # 变量说明：data 表示当前处理的数据。
    data: bytes


# 函数职责：完成 safe_name 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _safe_name(value: str | None) -> str:
    # 变量说明：name 表示当前对象名称。
    name = Path(str(value or "attachment").replace("\\", "/")).name.strip()
    return (name or "attachment")[:255]


# 函数职责：完成 mime_type 对应的业务处理。
# 参数关系：name 表示当前对象名称；supplied 表示当前步骤使用的 supplied 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _mime_type(name: str, supplied: str | None) -> str:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = str(supplied or "").split(";", 1)[0].strip().lower()
    if normalized and normalized != "application/octet-stream":
        return normalized[:120]
    # 变量说明：guessed 表示当前步骤使用的 guessed 值；_encoding 表示当前步骤使用的 _encoding 值。
    guessed, _encoding = mimetypes.guess_type(name)
    return str(guessed or "application/octet-stream")[:120]


# 函数职责：异步完成 read_uploaded_attachments 对应的业务处理。
# 参数关系：files 表示当前流程使用的 files 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def read_uploaded_attachments(files: Sequence[UploadFile]) -> list[UploadedAttachment]:
    """Read bounded multipart uploads without creating a workspace file."""

    if len(files) > MAX_ATTACHMENT_COUNT:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"每条消息最多附加 {MAX_ATTACHMENT_COUNT} 个文件",
        )
    # 变量说明：attachments 表示当前流程使用的 attachments 集合。
    attachments: list[UploadedAttachment] = []
    # 变量说明：total_bytes 表示当前流程使用的 total_bytes 集合。
    total_bytes = 0
    for upload in files:
        # 变量说明：chunks 表示当前流程使用的 chunks 集合。
        chunks: list[bytes] = []
        # 变量说明：file_bytes 表示当前流程使用的 file_bytes 集合。
        file_bytes = 0
        while chunk := await upload.read(1024 * 1024):
            file_bytes += len(chunk)
            total_bytes += len(chunk)
            if file_bytes > MAX_ATTACHMENT_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"附件 {upload.filename or 'attachment'} 超过 25 MB",
                )
            if total_bytes > MAX_ATTACHMENT_TOTAL_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="单条消息的附件总大小不能超过 75 MB",
                )
            chunks.append(chunk)
        # 变量说明：name 表示当前对象名称。
        name = _safe_name(upload.filename)
        attachments.append(UploadedAttachment(
            name=name,
            mime_type=_mime_type(name, upload.content_type),
            data=b"".join(chunks),
        ))
        await upload.close()
    return attachments


# 函数职责：完成 public_attachment 对应的业务处理。
# 参数关系：row 表示当前步骤使用的 row 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _public_attachment(row: Artifact) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "mime_type": row.mime_type or "application/octet-stream",
        "size_bytes": max(0, int(row.size_bytes or 0)),
        "kind": row.kind,
    }


# 函数职责：完成 persist_uploaded_attachments 对应的业务处理。
# 参数关系：db 表示当前数据库会话；session_id 表示所属会话标识；artifact_store 表示当前步骤使用的 artifact_store 值；uploads 表示当前流程使用的 uploads 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def persist_uploaded_attachments(
    db: Any,
    *,
    session_id: str,
    artifact_store: FilesystemArtifactStore,
    uploads: Sequence[UploadedAttachment],
) -> list[dict[str, Any]]:
    """Persist originals in the session artifact directory and return safe refs."""

    # 变量说明：records 表示当前流程使用的 records 集合。
    records: list[dict[str, Any]] = []
    for upload in uploads:
        # 变量说明：ref 表示当前步骤使用的 ref 值。
        ref = artifact_store.put(
            upload.data,
            kind="user_attachment",
            mime_type=upload.mime_type,
        )
        # 变量说明：row 表示当前步骤使用的 row 值。
        row = Artifact(
            session_id=session_id,
            kind="user_attachment",
            name=upload.name,
            storage_path=ref.storage_key,
            sha256=ref.sha256,
            mime_type=upload.mime_type,
            size_bytes=ref.size,
            preview=ref.preview if _is_text(upload.name, upload.mime_type) else "",
            metadata_json={"runtime_artifact_id": ref.artifact_id},
            status="available",
        )
        db.add(row)
        db.flush()
        records.append(_public_attachment(row))
    return records


# 函数职责：完成 attachment_inventory_text 对应的业务处理。
# 参数关系：attachments 表示当前流程使用的 attachments 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def attachment_inventory_text(attachments: Sequence[Mapping[str, Any]]) -> str:
    if not attachments:
        return ""
    # 变量说明：lines 表示当前流程使用的 lines 集合。
    lines = [
        "<session-attachments>",
        "这些附件属于当前会话的私有附件域，不在工作区中；不要用工作区文件工具猜测路径。",
    ]
    for item in attachments:
        lines.append(
            f"- id={item.get('id')} name={item.get('name')} "
            f"mime={item.get('mime_type')} size={int(item.get('size_bytes') or 0)}"
        )
    lines.extend([
        "图片已经作为视觉输入附在本消息中。文本和 PDF 请使用附件工具按需读取；扫描 PDF 可先渲染指定页面。",
        "</session-attachments>",
    ])
    return "\n".join(lines)


# 函数职责：完成 attachment_message_content 对应的业务处理。
# 参数关系：content 表示待处理或返回的正文内容；attachments 表示当前流程使用的 attachments 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def attachment_message_content(
    content: str,
    attachments: Sequence[Mapping[str, Any]],
) -> str | list[dict[str, Any]]:
    """Build durable provider content with ID refs, never embedded base64."""

    # 变量说明：inventory 表示当前步骤使用的 inventory 值。
    inventory = attachment_inventory_text(attachments)
    # 变量说明：text 表示当前步骤使用的 text 值。
    text = "\n\n".join(item for item in (content, inventory) if item).strip()
    # 变量说明：image_refs 表示当前流程使用的 image_refs 集合。
    image_refs = [
        item for item in attachments
        if str(item.get("mime_type") or "").lower() in _IMAGE_MIME_TYPES
    ]
    if not image_refs:
        return text
    # 变量说明：parts 表示当前流程使用的 parts 集合。
    parts: list[dict[str, Any]] = [{"type": "text", "text": text or "请分析随附图片。"}]
    parts.extend({
        "type": "pgagent_image_ref",
        "attachment_id": str(item.get("id") or ""),
        "name": str(item.get("name") or "image"),
        "mime_type": str(item.get("mime_type") or "image/png"),
    } for item in image_refs)
    return parts


# 函数职责：完成 is_text 对应的业务处理。
# 参数关系：name 表示当前对象名称；mime_type 表示当前步骤使用的 mime_type 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _is_text(name: str, mime_type: str) -> bool:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = mime_type.lower()
    return (
        normalized.startswith("text/")
        or normalized in {"application/json", "application/xml", "application/javascript"}
        or Path(name).suffix.lower() in _TEXT_EXTENSIONS
    )


# 函数职责：完成 decode_text 对应的业务处理。
# 参数关系：data 表示当前处理的数据。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _decode_text(data: bytes) -> str | None:
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


# 类职责：封装 AttachmentToolStore 的持久化访问。
class AttachmentToolStore:
    """Resolve only attachment rows owned by one bound session."""

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：session_id 表示所属会话标识；artifact_store 表示当前步骤使用的 artifact_store 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self, session_id: str, artifact_store: FilesystemArtifactStore) -> None:
        # 变量说明：session_id 表示所属会话标识。
        self.session_id = str(session_id)
        # 变量说明：artifact_store 表示当前步骤使用的 artifact_store 值。
        self.artifact_store = artifact_store

    # 函数职责：完成 row 对应的业务处理。
    # 参数关系：attachment_id 表示attachment 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _row(self, attachment_id: str) -> Artifact | None:
        with database_module.SessionLocal() as db:
            # 变量说明：row 表示当前步骤使用的 row 值。
            row = db.scalar(select(Artifact).where(
                Artifact.id == str(attachment_id or ""),
                Artifact.session_id == self.session_id,
                Artifact.kind.in_({"user_attachment", "attachment_derivative"}),
                Artifact.status == "available",
            ))
            if row is None:
                return None
            db.expunge(row)
            return row

    # 函数职责：完成 payload 对应的业务处理。
    # 参数关系：row 表示当前步骤使用的 row 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _payload(self, row: Artifact) -> bytes | None:
        # 变量说明：metadata 表示当前步骤使用的 metadata 值。
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        # 变量说明：runtime_id 表示runtime 对象的唯一标识。
        runtime_id = str(metadata.get("runtime_artifact_id") or "")
        if not runtime_id:
            return None
        try:
            return self.artifact_store.get(runtime_id)
        except (OSError, ValueError):
            return None

    # 函数职责：完成 data_url 对应的业务处理。
    # 参数关系：attachment_id 表示attachment 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def data_url(self, attachment_id: str) -> str | None:
        # 变量说明：row 表示当前步骤使用的 row 值。
        row = self._row(attachment_id)
        if row is None or str(row.mime_type or "").lower() not in _IMAGE_MIME_TYPES:
            return None
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = self._payload(row)
        if payload is None:
            return None
        # 变量说明：encoded 表示当前步骤使用的 encoded 值。
        encoded = base64.b64encode(payload).decode("ascii")
        return f"data:{row.mime_type};base64,{encoded}"

    # 函数职责：完成 content 对应的业务处理。
    # 参数关系：attachment_id 表示attachment 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def content(self, attachment_id: str) -> tuple[Artifact, bytes] | None:
        # 变量说明：row 表示当前步骤使用的 row 值。
        row = self._row(attachment_id)
        if row is None:
            return None
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = self._payload(row)
        return (row, payload) if payload is not None else None

    # 函数职责：完成 list 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def list(self) -> ToolResult:
        with database_module.SessionLocal() as db:
            # 变量说明：rows 表示当前流程使用的 rows 集合。
            rows = list(db.scalars(select(Artifact).where(
                Artifact.session_id == self.session_id,
                Artifact.kind == "user_attachment",
                Artifact.status == "available",
            ).order_by(Artifact.created_at.asc(), Artifact.id.asc())))
        return ToolResult(
            "list_attachments",
            True,
            json.dumps([_public_attachment(row) for row in rows], ensure_ascii=False),
            metadata={"count": len(rows)},
        )

    # 函数职责：完成 info 对应的业务处理。
    # 参数关系：attachment_id 表示attachment 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def info(self, attachment_id: str) -> ToolResult:
        # 变量说明：row 表示当前步骤使用的 row 值。
        row = self._row(attachment_id)
        if row is None:
            return self._not_found("attachment_info")
        return ToolResult(
            "attachment_info",
            True,
            json.dumps(_public_attachment(row), ensure_ascii=False),
        )

    # 函数职责：完成 read 对应的业务处理。
    # 参数关系：attachment_id 表示attachment 对象的唯一标识；offset 表示当前步骤使用的 offset 值；limit 表示当前步骤使用的 limit 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def read(
        self,
        attachment_id: str,
        *,
        offset: int = 0,
        limit: int = DEFAULT_ATTACHMENT_PAGE_CHARS,
    ) -> ToolResult:
        try:
            # 变量说明：start 表示当前步骤使用的 start 值。
            start = int(offset)
            # 变量说明：requested 表示当前步骤使用的 requested 值。
            requested = int(limit)
        except (TypeError, ValueError):
            return self._invalid("read_attachment", "offset 和 limit 必须是整数")
        if start < 0 or requested < 1 or requested > MAX_ATTACHMENT_PAGE_CHARS:
            return self._invalid(
                "read_attachment",
                f"offset 必须大于等于 0，limit 必须在 1 到 {MAX_ATTACHMENT_PAGE_CHARS} 之间",
            )
        # 变量说明：resolved 表示当前步骤使用的 resolved 值。
        resolved = self.content(attachment_id)
        if resolved is None:
            return self._not_found("read_attachment")
        # 变量说明：row 表示当前步骤使用的 row 值；payload 表示跨层传递的数据载荷。
        row, payload = resolved
        if (row.mime_type or "").lower() == "application/pdf" or Path(row.name).suffix.lower() == ".pdf":
            return ToolResult(
                "read_attachment",
                False,
                "PDF 请使用 inspect_pdf；扫描页可使用 render_pdf_page。",
                error_code="pdf_requires_inspector",
            )
        if not _is_text(row.name, row.mime_type or ""):
            return ToolResult(
                "read_attachment",
                False,
                "该附件不是可分页读取的文本；图片会作为视觉输入提供，其他二进制格式需要专用处理器。",
                error_code="unsupported_attachment_type",
            )
        # 变量说明：text 表示当前步骤使用的 text 值。
        text = _decode_text(payload)
        if text is None:
            return ToolResult(
                "read_attachment",
                False,
                "附件内容不是 UTF-8/UTF-16 文本。",
                error_code="unsupported_text_encoding",
            )
        # 变量说明：end 表示当前步骤使用的 end 值。
        end = min(len(text), start + requested)
        return ToolResult(
            "read_attachment",
            True,
            text[start:end],
            metadata={
                "attachment_id": row.id,
                "offset": start,
                "next_offset": None if end >= len(text) else end,
                "total_chars": len(text),
                "eof": end >= len(text),
            },
        )

    # 函数职责：完成 inspect_pdf 对应的业务处理。
    # 参数关系：attachment_id 表示attachment 对象的唯一标识；start_page 表示当前步骤使用的 start_page 值；page_count 表示page 的数量。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def inspect_pdf(
        self,
        attachment_id: str,
        *,
        start_page: int = 1,
        page_count: int = 10,
    ) -> ToolResult:
        # 变量说明：resolved 表示当前步骤使用的 resolved 值。
        resolved = self.content(attachment_id)
        if resolved is None:
            return self._not_found("inspect_pdf")
        # 变量说明：row 表示当前步骤使用的 row 值；payload 表示跨层传递的数据载荷。
        row, payload = resolved
        if (row.mime_type or "").lower() != "application/pdf" and Path(row.name).suffix.lower() != ".pdf":
            return self._invalid("inspect_pdf", "指定附件不是 PDF")
        try:
            # 变量说明：first 表示当前步骤使用的 first 值。
            first = int(start_page)
            # 变量说明：count 表示当前步骤使用的 count 值。
            count = int(page_count)
        except (TypeError, ValueError):
            return self._invalid("inspect_pdf", "start_page 和 page_count 必须是整数")
        if first < 1 or count < 1 or count > MAX_PDF_PAGES_PER_READ:
            return self._invalid(
                "inspect_pdf",
                f"start_page 必须大于等于 1，page_count 必须在 1 到 {MAX_PDF_PAGES_PER_READ} 之间",
            )
        # 变量说明：fitz 表示当前步骤使用的 fitz 值。
        fitz = self._fitz("inspect_pdf")
        if isinstance(fitz, ToolResult):
            return fitz
        try:
            # 变量说明：document 表示当前步骤使用的 document 值。
            document = fitz.open(stream=payload, filetype="pdf")
            # 变量说明：total_pages 表示当前流程使用的 total_pages 集合。
            total_pages = document.page_count
            # 变量说明：last 表示当前步骤使用的 last 值。
            last = min(total_pages, first - 1 + count)
            # 变量说明：pages 表示当前流程使用的 pages 集合。
            pages = []
            for index in range(first - 1, last):
                # 变量说明：text 表示当前步骤使用的 text 值。
                text = document.load_page(index).get_text("text").strip()
                pages.append({"page": index + 1, "text": text})
            document.close()
        except Exception as exc:
            return ToolResult(
                "inspect_pdf",
                False,
                f"PDF 无法读取：{type(exc).__name__}",
                error_code="invalid_pdf",
            )
        # 变量说明：extracted 表示当前步骤使用的 extracted 值。
        extracted = sum(len(item["text"]) for item in pages)
        return ToolResult(
            "inspect_pdf",
            True,
            json.dumps({
                "attachment_id": row.id,
                "name": row.name,
                "total_pages": total_pages,
                "pages": pages,
                "note": (
                    "所选页面没有可提取文字；使用 render_pdf_page 将需要的页面作为图片观察。"
                    if not extracted else ""
                ),
            }, ensure_ascii=False),
            metadata={"attachment_id": row.id, "extracted_chars": extracted},
        )

    # 函数职责：完成 render_pdf_page 对应的业务处理。
    # 参数关系：attachment_id 表示attachment 对象的唯一标识；page 表示当前步骤使用的 page 值；scale 表示当前步骤使用的 scale 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def render_pdf_page(
        self,
        attachment_id: str,
        *,
        page: int = 1,
        scale: float = 1.6,
    ) -> ToolResult:
        # 变量说明：resolved 表示当前步骤使用的 resolved 值。
        resolved = self.content(attachment_id)
        if resolved is None:
            return self._not_found("render_pdf_page")
        # 变量说明：row 表示当前步骤使用的 row 值；payload 表示跨层传递的数据载荷。
        row, payload = resolved
        if (row.mime_type or "").lower() != "application/pdf" and Path(row.name).suffix.lower() != ".pdf":
            return self._invalid("render_pdf_page", "指定附件不是 PDF")
        try:
            # 变量说明：page_number 表示当前步骤使用的 page_number 值。
            page_number = int(page)
            # 变量说明：render_scale 表示当前步骤使用的 render_scale 值。
            render_scale = float(scale)
        except (TypeError, ValueError):
            return self._invalid("render_pdf_page", "page 和 scale 必须是数字")
        if page_number < 1 or not 0.75 <= render_scale <= 3.0:
            return self._invalid("render_pdf_page", "page 必须大于等于 1，scale 必须在 0.75 到 3.0 之间")
        # 变量说明：fitz 表示当前步骤使用的 fitz 值。
        fitz = self._fitz("render_pdf_page")
        if isinstance(fitz, ToolResult):
            return fitz
        try:
            # 变量说明：document 表示当前步骤使用的 document 值。
            document = fitz.open(stream=payload, filetype="pdf")
            # 变量说明：total_pages 表示当前流程使用的 total_pages 集合。
            total_pages = document.page_count
            if page_number > total_pages:
                document.close()
                return self._invalid("render_pdf_page", f"PDF 只有 {total_pages} 页")
            # 变量说明：pixmap 表示当前步骤使用的 pixmap 值。
            pixmap = document.load_page(page_number - 1).get_pixmap(
                matrix=fitz.Matrix(render_scale, render_scale),
                alpha=False,
            )
            # 变量说明：image_bytes 表示当前流程使用的 image_bytes 集合。
            image_bytes = pixmap.tobytes("png")
            document.close()
        except Exception as exc:
            return ToolResult(
                "render_pdf_page",
                False,
                f"PDF 页面渲染失败：{type(exc).__name__}",
                error_code="pdf_render_failed",
            )
        # 变量说明：ref 表示当前步骤使用的 ref 值。
        ref = self.artifact_store.put(
            image_bytes,
            kind="attachment_derivative",
            mime_type="image/png",
        )
        with database_module.SessionLocal() as db:
            # 变量说明：derivative 表示当前步骤使用的 derivative 值。
            derivative = Artifact(
                session_id=self.session_id,
                kind="attachment_derivative",
                name=f"{Path(row.name).stem}-page-{page_number}.png",
                storage_path=ref.storage_key,
                sha256=ref.sha256,
                mime_type="image/png",
                size_bytes=ref.size,
                preview="",
                metadata_json={
                    "runtime_artifact_id": ref.artifact_id,
                    "source_attachment_id": row.id,
                    "page": page_number,
                },
                status="available",
            )
            db.add(derivative)
            db.commit()
            db.refresh(derivative)
            # 变量说明：public 表示当前步骤使用的 public 值。
            public = _public_attachment(derivative)
        return ToolResult(
            "render_pdf_page",
            True,
            f"已将 {row.name} 第 {page_number} 页渲染为会话内图片 {derivative.id}，该图片会附在下一次模型观察中。",
            metadata={"model_image_ref": public, "source_attachment_id": row.id, "page": page_number},
        )

    # 函数职责：完成 fitz 对应的业务处理。
    # 参数关系：tool_name 表示当前步骤使用的 tool_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _fitz(tool_name: str) -> Any | ToolResult:
        try:
            import fitz
        except ImportError:
            return ToolResult(
                tool_name,
                False,
                "PDF 处理组件未安装，请安装 backend requirements。",
                error_code="pdf_processor_unavailable",
            )
        return fitz

    # 函数职责：完成 not_found 对应的业务处理。
    # 参数关系：tool_name 表示当前步骤使用的 tool_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _not_found(tool_name: str) -> ToolResult:
        return ToolResult(
            tool_name,
            False,
            "附件不存在或不属于当前会话",
            error_code="attachment_not_found",
        )

    # 函数职责：完成 invalid 对应的业务处理。
    # 参数关系：tool_name 表示当前步骤使用的 tool_name 值；message 表示当前消息。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _invalid(tool_name: str, message: str) -> ToolResult:
        return ToolResult(tool_name, False, message, error_code="invalid_arguments")
