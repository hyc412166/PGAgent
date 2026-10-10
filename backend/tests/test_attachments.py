"""验证附件上传、会话私有存储、草稿原子创建以及 PDF 文本提取和页面渲染。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

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
from src.model.gateway import bind_attachment_store
from src.api.runtime import _public_stream_event
from src.api.routes.shared import _public_run_event
from src.persistence.database import RunEvent
from src.runs.stream import run_stream_broker
from src.persistence import database
from src.persistence.database import (
    Artifact,
    Base,
    ChatMessage,
    ModelConnection,
    Run,
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
# 测试夹具：attachment_client 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def attachment_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, Path, list[str]]:
    # data_dir 是附件与派生产物的私有根目录；scheduled 收集草稿是否真正进入运行调度器。
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


# 辅助函数：_session 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
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


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_text_attachment_turn_stays_in_private_session_storage 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_image_attachment_history_keeps_reference_instead_of_base64 精确标识本用例的具体条件。
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


def test_runtime_only_reports_images_uploaded_in_current_turn(
    attachment_client: tuple[TestClient, Path, list[str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.runs import runtime_factory

    client, _data_dir, _launched = attachment_client
    session_id = _session()
    with database.SessionLocal() as db:
        connection = ModelConnection(name="Image test", provider="openai_compatible", base_url="https://example.invalid/v1",
                                     secret_ref="test:image", default_model="test-model", status="connected")
        db.add(connection)
        db.flush()
        db.get(Session, session_id).model_connection_id = connection.id
        db.commit()
    captured: list[list[dict]] = []

    async def model_call(**kwargs):
        captured.append(kwargs["messages"])
        return {"content": "ok"}

    monkeypatch.setattr(runtime_factory, "build_model_call", lambda _config: model_call)
    for index, image_name in enumerate(["first.png", None, "second.png"]):
        payload = {"content": "查看图片" if image_name else "纯文字继续", "idempotency_key": f"image-turn-{index}"}
        response = (client.post(f"/api/sessions/{session_id}/turns", data={"payload": json.dumps(payload)},
                               files=[("files", (image_name, b"\x89PNG\r\n\x1a\nimage-bytes", "image/png"))])
                    if image_name else client.post(f"/api/sessions/{session_id}/run", json=payload))
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        runtime, context = coordinator._resolve_runtime(run_id)
        asyncio.run(runtime.model_call(messages=context["recent_messages"]))
        asyncio.run(runtime.model_call(messages=context["recent_messages"]))
        with database.SessionLocal() as db:
            events = list(db.query(RunEvent).filter(RunEvent.run_id == run_id, RunEvent.event_type.like("image_read_%")))
            assert [event.event_type for event in events] == (["image_read_started", "image_read_completed"] if image_name else [])
            assert all(event.payload["image_name"] == image_name for event in events)
            db.get(Run, run_id).status = "completed"
            db.commit()
    # 纯文字续接仍保留先前图片上下文，但不得把协议重发呈现为新读取。
    assert captured[2][0]["content"][1]["type"] == "image_url"


def test_image_reads_reach_live_stream_and_public_history(
    attachment_client: tuple[TestClient, Path, list[str]],
) -> None:
    client, data_dir, _launched = attachment_client
    session_id = _session()
    response = client.post(f"/api/sessions/{session_id}/turns", data={"payload": json.dumps({
        "content": "看看这张图", "idempotency_key": "image-read-events",
    })}, files=[("files", ("sample.png", b"\x89PNG\r\n\x1a\nimage-bytes", "image/png"))])
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    with database.SessionLocal() as db:
        history = _prepare_session_history(db, db.get(Session, session_id))
    captured: dict = {}

    async def raw_model_call(**kwargs):
        captured.update(kwargs)
        return {"content": "ok"}

    store = AttachmentToolStore(session_id, FilesystemArtifactStore(data_dir / "artifacts" / session_id))
    call = bind_attachment_store(raw_model_call, store, image_event_sink=coordinator._event_sink(run_id))
    asyncio.run(call(messages=history))
    assert captured["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    with database.SessionLocal() as db:
        rows = list(db.query(RunEvent).filter(RunEvent.run_id == run_id).order_by(RunEvent.sequence.asc()))
    images = [_public_run_event(row) for row in rows if row.event_type.startswith("image_read_")]
    assert [row.event_type for row in images] == ["image_read_started", "image_read_completed"]
    assert images[0].payload["image_name"] == "sample.png"
    assert "base64" not in json.dumps([row.payload for row in images])

    async def replay_events() -> list[dict]:
        replay, subscription = run_stream_broker.subscribe(run_id)
        run_stream_broker.unsubscribe(subscription)
        return [_public_stream_event(event) for event in replay if event["type"].startswith("image_read_")]

    replay = asyncio.run(replay_events())
    assert [event["type"] for event in replay] == ["image_read_started", "image_read_completed"]
    assert replay[0]["attachment_id"] == images[0].payload["attachment_id"]

    # 公开投影只提供图片身份与显示名称，不透传图片内容或存储位置。
    unsafe = RunEvent(id="image-event", run_id=run_id, event_type="image_read_failed", payload={
        "attachment_id": "image-1", "image_name": "sample.png", "error_code": "image_unavailable",
        "data_url": "data:image/png;base64,private", "storage_path": "private/path", "content": "private-image",
    }, created_at=images[0].created_at, sequence=1)
    assert _public_run_event(unsafe).payload == {
        "attachment_id": "image-1", "image_name": "sample.png", "error_code": "image_unavailable",
    }


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_attachment_only_draft_is_created_atomically 精确标识本用例的具体条件。
def test_attachment_only_draft_is_created_atomically(
    attachment_client: tuple[TestClient, Path, list[str]],
) -> None:
    client, _data_dir, launched = attachment_client
    payload = {
        "idempotency_key": "draft-attachment-only",
        "title": "report.pdf",
        "content": "",
        "thinking_level": "medium",
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


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_pdf_tools_extract_text_and_render_a_private_image 精确标识本用例的具体条件。
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


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_pdf_render_is_read_only_for_approval_but_not_parallel 精确标识本用例的具体条件。
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
