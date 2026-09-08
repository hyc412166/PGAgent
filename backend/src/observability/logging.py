"""结构化诊断日志：把运行上下文写入本地 JSONL，并控制文件生命周期。"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from logging import LogRecord
from pathlib import Path
from typing import Any

from .context import current_observability_context
from .redaction import redact_mapping, redact_text


_FILE_NAME_RE = re.compile(r"^pgagent-\d{4}-\d{2}-\d{2}(?:\.\d+)?\.jsonl$")
_STANDARD_RECORD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)))


class JsonLineFormatter(logging.Formatter):
    """将日志记录编码为一行脱敏 JSON，便于人工和脚本同时检索。"""

    def format(self, record: LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage(), limit=8_000),
            "process_id": record.process,
            "thread_id": record.thread,
            **current_observability_context(),
        }
        event_type = getattr(record, "event_type", None)
        if isinstance(event_type, str) and event_type:
            payload["event_type"] = event_type
        fields = getattr(record, "observability_fields", None)
        if isinstance(fields, dict):
            payload.update(redact_mapping(fields, max_text_chars=8_000))
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info), limit=20_000)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class DailySizeRotatingFileHandler(logging.Handler):
    """按 UTC 日期和大小切分 JSONL 文件，并清理本处理器产生的旧文件。"""

    def __init__(self, log_dir: Path, *, max_bytes: int, retention_days: int) -> None:
        super().__init__()
        self.log_dir = Path(log_dir)
        self.max_bytes = max(1, int(max_bytes))
        self.retention_days = max(1, int(retention_days))
        self._stream = None
        self._date = ""
        self._part = 0
        self._lock = threading.RLock()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup(datetime.now(timezone.utc))

    def _path(self) -> Path:
        suffix = f".{self._part}" if self._part else ""
        return self.log_dir / f"pgagent-{self._date}{suffix}.jsonl"

    def _open_for_today(self, today: str) -> None:
        if self._stream is not None and self._date == today:
            return
        self._close_stream()
        self._date = today
        parts = sorted(self.log_dir.glob(f"pgagent-{today}*.jsonl"))
        self._part = 0
        if parts:
            last = parts[-1].name
            match = re.search(r"\.(\d+)\.jsonl$", last)
            self._part = int(match.group(1)) if match else 0
        self._stream = self._path().open("a", encoding="utf-8")

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def _rollover(self) -> None:
        self._close_stream()
        self._part += 1
        self._stream = self._path().open("a", encoding="utf-8")

    def _cleanup(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.retention_days)
        for path in self.log_dir.iterdir():
            if not path.is_file() or not _FILE_NAME_RE.fullmatch(path.name):
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if modified < cutoff:
                    path.unlink()
            except OSError:
                print(f"PGAgent log cleanup failed for {path}", file=sys.stderr)

    def emit(self, record: LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
            with self._lock:
                now = datetime.now(timezone.utc)
                self._open_for_today(now.strftime("%Y-%m-%d"))
                if self._stream is None:
                    return
                if self._stream.tell() + len(line.encode("utf-8")) > self.max_bytes and self._stream.tell() > 0:
                    self._rollover()
                self._stream.write(line)
                self._stream.flush()
        except Exception as exc:  # 日志不能反向阻塞 Agent 主流程。
            print(f"PGAgent diagnostic log write failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    def close(self) -> None:
        with self._lock:
            self._close_stream()
        super().close()


def configure_observability_logging(*, level: str, log_dir: Path, retention_days: int, max_bytes: int) -> None:
    """初始化根 logger；重复调用会替换本模块创建的 handler。"""

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        if getattr(handler, "_pgagent_observability", False):
            root.removeHandler(handler)
            handler.close()

    formatter = JsonLineFormatter()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._pgagent_observability = True  # type: ignore[attr-defined]
    file_handler = DailySizeRotatingFileHandler(log_dir, max_bytes=max_bytes, retention_days=retention_days)
    file_handler.setFormatter(formatter)
    file_handler._pgagent_observability = True  # type: ignore[attr-defined]
    root.addHandler(console)
    root.addHandler(file_handler)


def log_event(logger: logging.Logger, event_type: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """写入一个带事件类型和安全字段的诊断记录。"""

    logger.log(
        level,
        event_type,
        extra={"event_type": event_type, "observability_fields": redact_mapping(fields)},
    )


__all__ = ["DailySizeRotatingFileHandler", "JsonLineFormatter", "configure_observability_logging", "log_event"]
