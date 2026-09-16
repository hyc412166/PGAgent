"""验证按运行读取持久化工具结果的分页接口与隔离边界。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import router as resources_router
from src.persistence import database
from src.persistence.database import Base, ChatMessage, configure_database, init_db


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    configure_database(f"sqlite:///{(tmp_path / 'tool-results.db').as_posix()}")
    init_db()
    app = FastAPI()
    app.include_router(resources_router)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(bind=database.engine)


def _add_tool_result(
    *,
    session_id: str,
    run_id: str,
    tool_call_id: str,
    tool_name: str,
    content: str,
    sequence: int = 1,
) -> None:
    with database.SessionLocal() as db:
        db.add(ChatMessage(
            session_id=session_id,
            role="tool",
            content=content,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            sequence=sequence,
            extra={"runtime_run_id": run_id, "source": "runtime_transcript"},
            provider_payload={"private": "must-not-leak"},
        ))
        db.commit()


def test_inspect_pdf_tool_result_returns_the_complete_persisted_json_in_pages(
    client: TestClient,
) -> None:
    session = client.post("/api/sessions", json={}).json()
    run = client.post("/api/runs", json={"session_id": session["id"]}).json()
    pages = json.dumps({
        "pages": [
            {"page": 1, "text": "第一页" * 400},
            {"page": 2, "text": "第二页正文"},
        ],
    }, ensure_ascii=False)
    persisted = json.dumps({
        "tool_name": "inspect_pdf",
        "ok": True,
        "content": pages,
        "changed": False,
        "metadata": {"attachment_id": "attachment-1", "extracted_chars": 1_210, "private": "secret"},
    }, ensure_ascii=False)
    _add_tool_result(
        session_id=session["id"],
        run_id=run["id"],
        tool_call_id="inspect-pdf-call",
        tool_name="inspect_pdf",
        content=persisted,
    )

    first = client.get(
        f"/api/runs/{run['id']}/tool-results/inspect-pdf-call",
        params={"offset": 0, "limit": 1_200},
    )
    second = client.get(
        f"/api/runs/{run['id']}/tool-results/inspect-pdf-call",
        params={"offset": 1_200, "limit": 1_200},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_page = first.json()
    second_page = second.json()
    safe_text = first_page["content"] + second_page["content"]
    safe_result = json.loads(safe_text)
    assert safe_result == {
        "tool_name": "inspect_pdf",
        "ok": True,
        "content": pages,
        "changed": False,
        "metadata": {"attachment_id": "attachment-1", "extracted_chars": 1_210},
    }
    assert json.loads(safe_result["content"])["pages"][0]["text"] == "第一页" * 400
    assert first_page["offset"] == 0
    assert first_page["next_offset"] == 1_200
    assert first_page["eof"] is False
    assert second_page["next_offset"] == first_page["total_chars"]
    assert second_page["total_chars"] == first_page["total_chars"]
    assert second_page["eof"] is True
    assert "must-not-leak" not in first.text + second.text
    assert "secret" not in first.text + second.text


def test_read_artifact_tool_result_keeps_full_json_instead_of_event_summary(
    client: TestClient,
) -> None:
    session = client.post("/api/sessions", json={}).json()
    run = client.post("/api/runs", json={"session_id": session["id"]}).json()
    persisted = json.dumps({
        "tool_name": "read_artifact",
        "ok": True,
        "content": "完整 Artifact 正文",
        "changed": False,
        "metadata": {
            "artifact_id": "artifact-1",
            "offset": 0,
            "next_offset": None,
            "total_chars": 13,
            "eof": True,
        },
    }, ensure_ascii=False)
    _add_tool_result(
        session_id=session["id"],
        run_id=run["id"],
        tool_call_id="read-artifact-call",
        tool_name="read_artifact",
        content=persisted,
    )

    response = client.get(f"/api/runs/{run['id']}/tool-results/read-artifact-call")

    assert response.status_code == 200, response.text
    assert json.loads(response.json()["content"]) == json.loads(persisted)
    assert "完整 Artifact 正文" in response.json()["content"]


@pytest.mark.parametrize("tool_name", ["shell", "bash", "run_command"])
def test_shell_tool_result_removes_background_job_internal_fields(
    client: TestClient,
    tool_name: str,
) -> None:
    session = client.post("/api/sessions", json={}).json()
    run = client.post("/api/runs", json={"session_id": session["id"]}).json()
    background_payload = {
        "id": "job-secret",
        "session_id": "session-secret",
        "run_id": "run-secret",
        "status": "completed",
        "command": "Write-Output safe",
        "shell": "powershell",
        "cwd": "C:/private/workspace",
        "pid": 4312,
        "exit_code": 0,
        "log_path": "C:/private/data/background-jobs/job-secret.log",
        "output": "",
        "error": None,
        "created_at": "2026-09-15T10:00:00Z",
        "started_at": "2026-09-15T10:00:01Z",
        "finished_at": "2026-09-15T10:00:02Z",
        "observed_by_run_id": "observer-secret",
        "waiting_run_id": "waiting-secret",
        "next_output_offset": 0,
        "output_truncated": False,
        "truncated": False,
    }
    persisted = json.dumps({
        "tool_name": tool_name,
        "ok": True,
        "content": json.dumps(background_payload, ensure_ascii=False),
        "changed": False,
        "metadata": {
            "command": "Write-Output safe",
            "exit_code": 0,
            "log_path": background_payload["log_path"],
            "pid": 4312,
        },
        "approval_required": False,
        "approval_request": None,
    }, ensure_ascii=False)
    _add_tool_result(
        session_id=session["id"],
        run_id=run["id"],
        tool_call_id=f"{tool_name}-call",
        tool_name=tool_name,
        content=persisted,
    )

    response = client.get(f"/api/runs/{run['id']}/tool-results/{tool_name}-call")

    assert response.status_code == 200, response.text
    projected = json.loads(response.json()["content"])
    assert projected == {
        "tool_name": tool_name,
        "ok": True,
        "content": json.dumps({
            "command": "Write-Output safe",
                "shell": "powershell",
                "exit_code": 0,
                "output": "",
                "error": None,
                "output_truncated": False,
            "truncated": False,
        }, ensure_ascii=False),
        "changed": False,
        "metadata": {"command": "Write-Output safe", "exit_code": 0},
    }
    for private_value in (
        "job-secret", "session-secret", "run-secret", "C:/private/workspace",
        "4312", "C:/private/data", "2026-09-15", "observer-secret", "waiting-secret",
    ):
        assert private_value not in response.text
    for internal_field in (
        "log_path", "session_id", "run_id", "pid", "observed_by_run_id",
        "waiting_run_id", "created_at", "started_at", "finished_at", "cwd",
        "next_output_offset",
    ):
        assert internal_field not in response.text


def test_read_artifact_recursively_projects_a_complete_nested_shell_result(
    client: TestClient,
) -> None:
    session = client.post("/api/sessions", json={}).json()
    run = client.post("/api/runs", json={"session_id": session["id"]}).json()
    nested = {
        "tool_name": "shell",
        "ok": True,
        "content": json.dumps({
            "id": "nested-job-secret",
            "run_id": "nested-run-secret",
            "log_path": "C:/private/nested.log",
            "pid": 9123,
            "command": "echo nested",
            "shell": "powershell",
            "exit_code": 0,
            "output": "nested output",
            "error": None,
        }, ensure_ascii=False),
        "changed": False,
        "metadata": {"command": "echo nested", "log_path": "C:/private/nested.log"},
    }
    persisted = json.dumps({
        "tool_name": "read_artifact",
        "ok": True,
        "content": json.dumps(nested, ensure_ascii=False),
        "changed": False,
        "metadata": {
            "artifact_id": "artifact-1",
            "offset": 0,
            "next_offset": None,
            "total_chars": 400,
            "eof": True,
            "storage_path": "C:/private/artifact.json",
        },
    }, ensure_ascii=False)
    _add_tool_result(
        session_id=session["id"],
        run_id=run["id"],
        tool_call_id="nested-artifact-call",
        tool_name="read_artifact",
        content=persisted,
    )

    response = client.get(f"/api/runs/{run['id']}/tool-results/nested-artifact-call")

    assert response.status_code == 200, response.text
    outer = json.loads(response.json()["content"])
    inner = json.loads(outer["content"])
    shell_payload = json.loads(inner["content"])
    assert shell_payload == {
        "command": "echo nested",
        "shell": "powershell",
        "exit_code": 0,
        "output": "nested output",
        "error": None,
    }
    assert outer["metadata"] == {
        "artifact_id": "artifact-1",
        "offset": 0,
        "next_offset": None,
        "total_chars": 400,
        "eof": True,
    }
    assert "nested-job-secret" not in response.text
    assert "nested-run-secret" not in response.text
    assert "C:/private" not in response.text
    assert "9123" not in response.text


@pytest.mark.parametrize("persisted", ["[]", "null", "42", '"text"'])
def test_tool_result_accepts_valid_non_object_json_without_a_server_error(
    client: TestClient,
    persisted: str,
) -> None:
    session = client.post("/api/sessions", json={}).json()
    run = client.post("/api/runs", json={"session_id": session["id"]}).json()
    _add_tool_result(
        session_id=session["id"],
        run_id=run["id"],
        tool_call_id="non-object-call",
        tool_name="inspect_pdf",
        content=persisted,
    )

    response = client.get(f"/api/runs/{run['id']}/tool-results/non-object-call")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "tool_call_id": "non-object-call",
        "tool_name": "inspect_pdf",
        "ok": False,
        "content": persisted,
        "offset": 0,
        "next_offset": len(persisted),
        "total_chars": len(persisted),
        "eof": True,
    }


def test_tool_result_cannot_be_read_through_another_run_in_the_same_session(
    client: TestClient,
) -> None:
    session = client.post("/api/sessions", json={}).json()
    owner_run = client.post("/api/runs", json={"session_id": session["id"]}).json()
    other_run = client.post("/api/runs", json={"session_id": session["id"]}).json()
    _add_tool_result(
        session_id=session["id"],
        run_id=owner_run["id"],
        tool_call_id="private-call",
        tool_name="inspect_pdf",
        content='{"tool_name":"inspect_pdf","ok":true,"content":"private"}',
    )

    response = client.get(f"/api/runs/{other_run['id']}/tool-results/private-call")

    assert response.status_code == 404
    assert response.json() == {"detail": "Tool result not found"}


@pytest.mark.parametrize(
    ("params", "expected_status"),
    [
        ({"offset": -1}, 422),
        ({"limit": 0}, 422),
        ({"limit": 24_001}, 422),
        ({"offset": 0, "limit": 24_000}, 200),
    ],
)
def test_tool_result_offset_and_limit_boundaries(
    client: TestClient,
    params: dict[str, int],
    expected_status: int,
) -> None:
    session = client.post("/api/sessions", json={}).json()
    run = client.post("/api/runs", json={"session_id": session["id"]}).json()
    _add_tool_result(
        session_id=session["id"],
        run_id=run["id"],
        tool_call_id="boundary-call",
        tool_name="inspect_pdf",
        content='{"tool_name":"inspect_pdf","ok":true,"content":"page"}',
    )

    response = client.get(
        f"/api/runs/{run['id']}/tool-results/boundary-call",
        params=params,
    )

    assert response.status_code == expected_status, response.text
