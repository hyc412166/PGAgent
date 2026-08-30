from __future__ import annotations

import asyncio
import json
from pathlib import Path

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import runtime as runtime_api
from src.attachments.api import router as attachments_router
from src.attachments.storage import (
    AttachmentToolStore,
    UploadedAttachment,
    persist_uploaded_attachments,
)
from src.context.assembly import FilesystemArtifactStore
from src.context.compaction import _prepare_session_history
from src.persistence import database
from src.persistence.database import (
    Artifact,
    Base,
    ChatMessage,
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    Session,
    Workspace,
    configure_database,
    init_db,
)
from src.runs.service import coordinator
from src.tools.registry import create_default_registry


@pytest.fixture()
def attachment_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, Path, list[str]]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(
        type(runtime_api.settings),
        "data_dir",
        property(lambda _settings: data_dir),
    )
    configure_database(f"sqlite:///{(tmp_path / 'attachments.db').as_posix()}")
    init_db()
    launched: list[str] = []
    monkeypatch.setattr(
        coordinator,
        "launch",
        lambda run_id, resume=False: launched.append(run_id) or True,
    )
    app = FastAPI()
    app.include_router(runtime_api.router)
    app.include_router(attachments_router)
    with TestClient(app) as client:
        yield client, data_dir, launched
    Base.metadata.drop_all(bind=database.engine)


def _session() -> str:
    with database.SessionLocal() as db:
        session = Session(
            title="Attachment chat",
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id=DEFAULT_AGENT_ID,
        )
        db.add(session)
        db.commit()
        return session.id


def test_text_attachment_turn_stays_in_private_session_storage(
    attachment_client: tuple[TestClient, Path, list[str]],
) -> None:
    client, data_dir, launched = attachment_client
    session_id = _session()
    file_name = "private-once-note.txt"
    response = client.post(
        f"/api/sessions/{session_id}/turns",
        data={"payload": json.dumps({
            "content": "概括这个附件",
            "idempotency_key": "turn-with-text-file",
        })},
        files=[("files", (file_name, "line one\nline two".encode(), "text/plain"))],
    )

    assert response.status_code == 202, response.text
    assert launched == [response.json()["id"]]
    with database.SessionLocal() as db:
        message = db.query(ChatMessage).filter_by(session_id=session_id, role="user").one()
        attachment = db.query(Artifact).filter_by(session_id=session_id, kind="user_attachment").one()
        workspace = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        assert message.extra["attachments"] == [{
            "id": attachment.id,
            "name": file_name,
            "mime_type": "text/plain",
            "size_bytes": 17,
            "kind": "user_attachment",
        }]
        assert message.provider_payload["attachment_refs"] == message.extra["attachments"]
        assert Path(attachment.storage_path).is_relative_to(data_dir / "artifacts" / session_id)
        assert not (Path(workspace.root_path) / file_name).exists()
        history = _prepare_session_history(db, db.get(Session, session_id))
        assert history[0]["role"] == "user"
        assert file_name in history[0]["content"]
        assert "不在工作区中" in history[0]["content"]

    content = client.get(f"/api/sessions/{session_id}/attachments/{attachment.id}/content")
    assert content.status_code == 200
    assert content.text == "line one\nline two"

    store = AttachmentToolStore(
        session_id,
        FilesystemArtifactStore(data_dir / "artifacts" / session_id),
    )
    result = store.read(attachment.id, offset=5, limit=8)
    assert result.ok
    assert result.content == "one\nline"


def test_image_attachment_history_keeps_reference_instead_of_base64(
    attachment_client: tuple[TestClient, Path, list[str]],
) -> None:
    client, _data_dir, _launched = attachment_client
    session_id = _session()
    response = client.post(
        f"/api/sessions/{session_id}/turns",
        data={"payload": json.dumps({
            "content": "看看这张图",
            "idempotency_key": "turn-with-image",
        })},
        files=[("files", ("sample.png", b"\x89PNG\r\n\x1a\nimage-bytes", "image/png"))],
    )
    assert response.status_code == 202, response.text

    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        history = _prepare_session_history(db, session)
        content = history[0]["content"]
        assert isinstance(content, list)
        assert content[1]["type"] == "pgagent_image_ref"
        assert "base64" not in json.dumps(content)


def test_attachment_only_draft_is_created_atomically(
    attachment_client: tuple[TestClient, Path, list[str]],
) -> None:
    client, _data_dir, launched = attachment_client
    payload = {
        "idempotency_key": "draft-attachment-only",
        "title": "report.pdf",
        "content": "",
        "thinking_level": "auto",
        "permission_mode": "smart",
        "use_memories": True,
        "skill_ids": [],
        "mcp_server_names": [],
    }
    response = client.post(
        "/api/drafts/launch-input",
        data={"payload": json.dumps(payload)},
        files=[("files", ("report.txt", b"draft attachment", "text/plain"))],
    )
    assert response.status_code == 202, response.text
    session_id = response.json()["session"]["id"]
    assert launched == [response.json()["run"]["id"]]
    with database.SessionLocal() as db:
        assert db.query(Artifact).filter_by(session_id=session_id, kind="user_attachment").count() == 1
        message = db.query(ChatMessage).filter_by(session_id=session_id, role="user").one()
        assert message.content == ""
        assert message.extra["attachments"][0]["name"] == "report.txt"


def test_pdf_tools_extract_text_and_render_a_private_image(
    attachment_client: tuple[TestClient, Path, list[str]],
) -> None:
    _client, data_dir, _launched = attachment_client
    session_id = _session()
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Private PDF page")
    pdf_bytes = document.tobytes()
    document.close()

    artifact_store = FilesystemArtifactStore(data_dir / "artifacts" / session_id)
    with database.SessionLocal() as db:
        refs = persist_uploaded_attachments(
            db,
            session_id=session_id,
            artifact_store=artifact_store,
            uploads=[UploadedAttachment("private.pdf", "application/pdf", pdf_bytes)],
        )
        db.commit()
    attachment_id = refs[0]["id"]
    tools = AttachmentToolStore(session_id, artifact_store)

    inspected = tools.inspect_pdf(attachment_id, start_page=1, page_count=1)
    assert inspected.ok
    assert "Private PDF page" in inspected.content

    rendered = tools.render_pdf_page(attachment_id, page=1)
    assert rendered.ok
    image_ref = rendered.metadata["model_image_ref"]
    assert image_ref["mime_type"] == "image/png"
    assert tools.data_url(image_ref["id"]).startswith("data:image/png;base64,")
    with database.SessionLocal() as db:
        derivative = db.get(Artifact, image_ref["id"])
        assert derivative.kind == "attachment_derivative"
        assert Path(derivative.storage_path).is_relative_to(data_dir / "artifacts" / session_id)


def test_pdf_render_is_read_only_for_approval_but_not_parallel(
    attachment_client: tuple[TestClient, Path, list[str]],
) -> None:
    _client, data_dir, _launched = attachment_client
    session_id = _session()
    store = AttachmentToolStore(
        session_id,
        FilesystemArtifactStore(data_dir / "artifacts" / session_id),
    )
    registry = create_default_registry(
        str(data_dir),
        allowed_tool_names=["render_pdf_page"],
        attachment_store=store,
    )

    result = asyncio.run(registry.execute_async(
        "render_pdf_page",
        {"attachment_id": "missing", "page": 1},
    ))

    assert result.error_code == "attachment_not_found"
    assert not result.approval_required
    assert not registry.can_execute_batch_in_parallel(["render_pdf_page", "render_pdf_page"])
