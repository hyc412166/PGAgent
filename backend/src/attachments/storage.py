"""Store and inspect files that belong to one conversation, not its workspace."""

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


MAX_ATTACHMENT_COUNT = 10
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = 75 * 1024 * 1024
DEFAULT_ATTACHMENT_PAGE_CHARS = 20_000
MAX_ATTACHMENT_PAGE_CHARS = 24_000
MAX_PDF_PAGES_PER_READ = 20

_IMAGE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})
_TEXT_EXTENSIONS = frozenset({
    ".c", ".cc", ".conf", ".cpp", ".cs", ".css", ".csv", ".env", ".go",
    ".h", ".hpp", ".htm", ".html", ".ini", ".java", ".js", ".json",
    ".jsx", ".log", ".md", ".mjs", ".py", ".rb", ".rs", ".sh", ".sql",
    ".svg", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
})


@dataclass(frozen=True, slots=True)
class UploadedAttachment:
    name: str
    mime_type: str
    data: bytes


def _safe_name(value: str | None) -> str:
    name = Path(str(value or "attachment").replace("\\", "/")).name.strip()
    return (name or "attachment")[:255]


def _mime_type(name: str, supplied: str | None) -> str:
    normalized = str(supplied or "").split(";", 1)[0].strip().lower()
    if normalized and normalized != "application/octet-stream":
        return normalized[:120]
    guessed, _encoding = mimetypes.guess_type(name)
    return str(guessed or "application/octet-stream")[:120]


async def read_uploaded_attachments(files: Sequence[UploadFile]) -> list[UploadedAttachment]:
    """Read bounded multipart uploads without creating a workspace file."""

    if len(files) > MAX_ATTACHMENT_COUNT:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"每条消息最多附加 {MAX_ATTACHMENT_COUNT} 个文件",
        )
    attachments: list[UploadedAttachment] = []
    total_bytes = 0
    for upload in files:
        chunks: list[bytes] = []
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
        name = _safe_name(upload.filename)
        attachments.append(UploadedAttachment(
            name=name,
            mime_type=_mime_type(name, upload.content_type),
            data=b"".join(chunks),
        ))
        await upload.close()
    return attachments


def _public_attachment(row: Artifact) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "mime_type": row.mime_type or "application/octet-stream",
        "size_bytes": max(0, int(row.size_bytes or 0)),
        "kind": row.kind,
    }


def persist_uploaded_attachments(
    db: Any,
    *,
    session_id: str,
    artifact_store: FilesystemArtifactStore,
    uploads: Sequence[UploadedAttachment],
) -> list[dict[str, Any]]:
    """Persist originals in the session artifact directory and return safe refs."""

    records: list[dict[str, Any]] = []
    for upload in uploads:
        ref = artifact_store.put(
            upload.data,
            kind="user_attachment",
            mime_type=upload.mime_type,
        )
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


def attachment_inventory_text(attachments: Sequence[Mapping[str, Any]]) -> str:
    if not attachments:
        return ""
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


def attachment_message_content(
    content: str,
    attachments: Sequence[Mapping[str, Any]],
) -> str | list[dict[str, Any]]:
    """Build durable provider content with ID refs, never embedded base64."""

    inventory = attachment_inventory_text(attachments)
    text = "\n\n".join(item for item in (content, inventory) if item).strip()
    image_refs = [
        item for item in attachments
        if str(item.get("mime_type") or "").lower() in _IMAGE_MIME_TYPES
    ]
    if not image_refs:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text or "请分析随附图片。"}]
    parts.extend({
        "type": "pgagent_image_ref",
        "attachment_id": str(item.get("id") or ""),
        "name": str(item.get("name") or "image"),
        "mime_type": str(item.get("mime_type") or "image/png"),
    } for item in image_refs)
    return parts


def _is_text(name: str, mime_type: str) -> bool:
    normalized = mime_type.lower()
    return (
        normalized.startswith("text/")
        or normalized in {"application/json", "application/xml", "application/javascript"}
        or Path(name).suffix.lower() in _TEXT_EXTENSIONS
    )


def _decode_text(data: bytes) -> str | None:
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


class AttachmentToolStore:
    """Resolve only attachment rows owned by one bound session."""

    def __init__(self, session_id: str, artifact_store: FilesystemArtifactStore) -> None:
        self.session_id = str(session_id)
        self.artifact_store = artifact_store

    def _row(self, attachment_id: str) -> Artifact | None:
        with database_module.SessionLocal() as db:
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

    def _payload(self, row: Artifact) -> bytes | None:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        runtime_id = str(metadata.get("runtime_artifact_id") or "")
        if not runtime_id:
            return None
        try:
            return self.artifact_store.get(runtime_id)
        except (OSError, ValueError):
            return None

    def data_url(self, attachment_id: str) -> str | None:
        row = self._row(attachment_id)
        if row is None or str(row.mime_type or "").lower() not in _IMAGE_MIME_TYPES:
            return None
        payload = self._payload(row)
        if payload is None:
            return None
        encoded = base64.b64encode(payload).decode("ascii")
        return f"data:{row.mime_type};base64,{encoded}"

    def content(self, attachment_id: str) -> tuple[Artifact, bytes] | None:
        row = self._row(attachment_id)
        if row is None:
            return None
        payload = self._payload(row)
        return (row, payload) if payload is not None else None

    def list(self) -> ToolResult:
        with database_module.SessionLocal() as db:
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

    def info(self, attachment_id: str) -> ToolResult:
        row = self._row(attachment_id)
        if row is None:
            return self._not_found("attachment_info")
        return ToolResult(
            "attachment_info",
            True,
            json.dumps(_public_attachment(row), ensure_ascii=False),
        )

    def read(
        self,
        attachment_id: str,
        *,
        offset: int = 0,
        limit: int = DEFAULT_ATTACHMENT_PAGE_CHARS,
    ) -> ToolResult:
        try:
            start = int(offset)
            requested = int(limit)
        except (TypeError, ValueError):
            return self._invalid("read_attachment", "offset 和 limit 必须是整数")
        if start < 0 or requested < 1 or requested > MAX_ATTACHMENT_PAGE_CHARS:
            return self._invalid(
                "read_attachment",
                f"offset 必须大于等于 0，limit 必须在 1 到 {MAX_ATTACHMENT_PAGE_CHARS} 之间",
            )
        resolved = self.content(attachment_id)
        if resolved is None:
            return self._not_found("read_attachment")
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
        text = _decode_text(payload)
        if text is None:
            return ToolResult(
                "read_attachment",
                False,
                "附件内容不是 UTF-8/UTF-16 文本。",
                error_code="unsupported_text_encoding",
            )
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

    def inspect_pdf(
        self,
        attachment_id: str,
        *,
        start_page: int = 1,
        page_count: int = 10,
    ) -> ToolResult:
        resolved = self.content(attachment_id)
        if resolved is None:
            return self._not_found("inspect_pdf")
        row, payload = resolved
        if (row.mime_type or "").lower() != "application/pdf" and Path(row.name).suffix.lower() != ".pdf":
            return self._invalid("inspect_pdf", "指定附件不是 PDF")
        try:
            first = int(start_page)
            count = int(page_count)
        except (TypeError, ValueError):
            return self._invalid("inspect_pdf", "start_page 和 page_count 必须是整数")
        if first < 1 or count < 1 or count > MAX_PDF_PAGES_PER_READ:
            return self._invalid(
                "inspect_pdf",
                f"start_page 必须大于等于 1，page_count 必须在 1 到 {MAX_PDF_PAGES_PER_READ} 之间",
            )
        fitz = self._fitz("inspect_pdf")
        if isinstance(fitz, ToolResult):
            return fitz
        try:
            document = fitz.open(stream=payload, filetype="pdf")
            total_pages = document.page_count
            last = min(total_pages, first - 1 + count)
            pages = []
            for index in range(first - 1, last):
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

    def render_pdf_page(
        self,
        attachment_id: str,
        *,
        page: int = 1,
        scale: float = 1.6,
    ) -> ToolResult:
        resolved = self.content(attachment_id)
        if resolved is None:
            return self._not_found("render_pdf_page")
        row, payload = resolved
        if (row.mime_type or "").lower() != "application/pdf" and Path(row.name).suffix.lower() != ".pdf":
            return self._invalid("render_pdf_page", "指定附件不是 PDF")
        try:
            page_number = int(page)
            render_scale = float(scale)
        except (TypeError, ValueError):
            return self._invalid("render_pdf_page", "page 和 scale 必须是数字")
        if page_number < 1 or not 0.75 <= render_scale <= 3.0:
            return self._invalid("render_pdf_page", "page 必须大于等于 1，scale 必须在 0.75 到 3.0 之间")
        fitz = self._fitz("render_pdf_page")
        if isinstance(fitz, ToolResult):
            return fitz
        try:
            document = fitz.open(stream=payload, filetype="pdf")
            total_pages = document.page_count
            if page_number > total_pages:
                document.close()
                return self._invalid("render_pdf_page", f"PDF 只有 {total_pages} 页")
            pixmap = document.load_page(page_number - 1).get_pixmap(
                matrix=fitz.Matrix(render_scale, render_scale),
                alpha=False,
            )
            image_bytes = pixmap.tobytes("png")
            document.close()
        except Exception as exc:
            return ToolResult(
                "render_pdf_page",
                False,
                f"PDF 页面渲染失败：{type(exc).__name__}",
                error_code="pdf_render_failed",
            )
        ref = self.artifact_store.put(
            image_bytes,
            kind="attachment_derivative",
            mime_type="image/png",
        )
        with database_module.SessionLocal() as db:
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
            public = _public_attachment(derivative)
        return ToolResult(
            "render_pdf_page",
            True,
            f"已将 {row.name} 第 {page_number} 页渲染为会话内图片 {derivative.id}，该图片会附在下一次模型观察中。",
            metadata={"model_image_ref": public, "source_attachment_id": row.id, "page": page_number},
        )

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

    @staticmethod
    def _not_found(tool_name: str) -> ToolResult:
        return ToolResult(
            tool_name,
            False,
            "附件不存在或不属于当前会话",
            error_code="attachment_not_found",
        )

    @staticmethod
    def _invalid(tool_name: str, message: str) -> ToolResult:
        return ToolResult(tool_name, False, message, error_code="invalid_arguments")
