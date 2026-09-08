"""可观察性基础能力的行为测试：先验证上下文传播，再验证诊断数据边界。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from src.observability.context import (
    JSONScalar,
    bind_observability_context,
    current_observability_context,
)
from src.observability.redaction import (
    redact_mapping,
    redact_text,
    summarize_command,
)


async def _read_context() -> dict[str, JSONScalar]:
    """读取当前任务上下文，确认 contextvars 会随 asyncio task 传播。"""

    return current_observability_context()


async def _test_observability_context_propagates_and_restores() -> None:
    assert current_observability_context() == {}
    with bind_observability_context(
        trace_id="trace-1",
        run_id="run-1",
        step=2,
        ignored="must-not-be-bound",
        empty=None,
    ):
        inherited = await asyncio.create_task(_read_context())
        assert inherited == {"trace_id": "trace-1", "run_id": "run-1", "step": 2}

        with bind_observability_context(tool_call_id="call-1"):
            assert current_observability_context()["tool_call_id"] == "call-1"

        assert current_observability_context() == {
            "trace_id": "trace-1",
            "run_id": "run-1",
            "step": 2,
        }

    assert current_observability_context() == {}


def test_observability_context_propagates_and_restores() -> None:
    """用标准库驱动异步断言，避免测试依赖额外的 pytest 插件。"""

    asyncio.run(_test_observability_context_propagates_and_restores())


def test_redaction_removes_secret_fields_and_text_patterns(tmp_path: Path) -> None:
    source = {
        "Authorization": "Bearer abc.def.ghi",
        "nested": {
            "api_key": "sk-secret",
            "safe": "visible",
            "items": [
                {"PASSWORD": "nested-password"},
                "Bearer list-token",
            ],
        },
        "error": (
            "request token=top-secret "
            "url=https://x.test/a?api_key=query-secret&keep=yes "
            "db=postgresql://alice:db-secret@db.test/app "
            "API_KEY=env-secret "
            "jwt=eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.jwt-secret"
        ),
    }

    redacted = redact_mapping(source)

    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["nested"] == {
        "api_key": "[REDACTED]",
        "safe": "visible",
        "items": [{"PASSWORD": "[REDACTED]"}, "Bearer [REDACTED]"],
    }
    serialized = json.dumps(redacted)
    for secret in (
        "sk-secret",
        "nested-password",
        "top-secret",
        "query-secret",
        "db-secret",
        "env-secret",
        "jwt-secret",
        "api_key=query-secret",
    ):
        assert secret not in serialized


def test_redaction_preserves_non_secret_diagnostic_token_fields() -> None:
    redacted = redact_mapping(
        {
            "input_tokens": 12,
            "output_tokens": 3,
            "token_count": 15,
            "tokenizer": "cl100k_base",
            "secretary": "alice",
            "access_token": "hidden",
        }
    )

    assert redacted == {
        "input_tokens": 12,
        "output_tokens": 3,
        "token_count": 15,
        "tokenizer": "cl100k_base",
        "secretary": "alice",
        "access_token": "[REDACTED]",
    }


def test_redact_text_removes_nuls_and_bounds_length() -> None:
    value = (
        "\x00Bearer bearer-secret "
        "JWT eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.jwt-secret "
        "https://example.test/?token=query-secret&ok=1 "
        "postgresql://user:connection-secret@example.test/db "
        "OPENAI_API_KEY=environment-secret "
        + "x" * 200
    )

    redacted = redact_text(value, limit=120)

    assert "\x00" not in redacted
    assert len(redacted) <= 120
    for secret in (
        "bearer-secret",
        "jwt-secret",
        "query-secret",
        "connection-secret",
        "environment-secret",
    ):
        assert secret not in redacted


def test_redact_text_scrubs_colon_and_json_secret_patterns() -> None:
    redacted = redact_text(
        'Authorization: Basic abc123 {"api_key":"json-secret"} token: colon-secret'
    )

    assert "abc123" not in redacted
    assert "json-secret" not in redacted
    assert "colon-secret" not in redacted


def test_redact_text_scrubs_secret_assignments_with_auth_schemes() -> None:
    redacted = redact_text(
        'token=Bearer live-secret authorization=Basic basic-secret '
        'headers={"Authorization":"Bearer nested-secret"}'
    )

    assert "live-secret" not in redacted
    assert "basic-secret" not in redacted
    assert "nested-secret" not in redacted


def test_summarize_command_exposes_only_safe_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "backend"
    cwd.mkdir(parents=True)

    summary = summarize_command(
        ["python", "-c", "PASSWORD=do-not-log"],
        cwd=str(cwd),
        workspace_root=str(workspace),
    )

    assert summary == {
        "executable": "python",
        "argument_count": 2,
        "cwd": "backend",
    }
    assert set(summary) == {"executable", "argument_count", "cwd"}


def test_summarize_command_handles_string_and_cwd_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    summary = summarize_command(
        "python -m pytest",
        cwd=str(outside),
        workspace_root=str(tmp_path / "workspace"),
    )

    assert summary["executable"] == "python"
    assert summary["argument_count"] == 2
    assert summary["cwd"] == "<outside>"


def test_json_line_formatter_includes_context_and_redacts_secrets() -> None:
    from src.observability.logging import JsonLineFormatter

    formatter = JsonLineFormatter()
    record = logging.LogRecord(
        name="src.agent.engine",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="provider failed token=hidden",
        args=(),
        exc_info=None,
    )
    record.event_type = "model_failed"

    with bind_observability_context(trace_id="trace-1", run_id="run-1", step=3):
        payload = json.loads(formatter.format(record))

    assert payload["trace_id"] == "trace-1"
    assert payload["run_id"] == "run-1"
    assert payload["step"] == 3
    assert payload["event_type"] == "model_failed"
    assert payload["level"] == "ERROR"
    assert "hidden" not in json.dumps(payload)


def test_daily_size_handler_rotates_and_removes_only_expired_pgagent_logs(tmp_path: Path) -> None:
    from src.observability.logging import DailySizeRotatingFileHandler, JsonLineFormatter

    expired = tmp_path / "pgagent-2000-01-01.jsonl"
    expired.write_text("old", encoding="utf-8")
    unrelated = tmp_path / "user-notes.jsonl"
    unrelated.write_text("keep", encoding="utf-8")
    old = time.time() - 20 * 86400
    os.utime(expired, (old, old))
    os.utime(unrelated, (old, old))

    handler = DailySizeRotatingFileHandler(tmp_path, max_bytes=220, retention_days=14)
    handler.setFormatter(JsonLineFormatter())
    logger = logging.getLogger("test.rotation")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    for index in range(8):
        logger.info("rotation record %s %s", index, "x" * 80)
    handler.close()

    created = sorted(tmp_path.glob("pgagent-*.jsonl"))
    assert len(created) >= 2
    assert not expired.exists()
    assert unrelated.exists()
    assert all(json.loads(line) for path in created for line in path.read_text(encoding="utf-8").splitlines())


def test_logging_settings_have_safe_local_defaults() -> None:
    from src.config.settings import Settings

    configured = Settings(_env_file=None)

    assert configured.log_level == "INFO"
    assert configured.resolved_log_dir == configured.data_dir / "logs"
    assert configured.log_retention_days == 14
    assert configured.log_max_bytes == 20 * 1024 * 1024
