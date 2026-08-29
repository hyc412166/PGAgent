from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.config import settings
from src.context import instructions as instruction_service


@pytest.fixture(autouse=True)
def isolate_personal_agents_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    data_dir = tmp_path / "pgagent-data"
    data_dir.mkdir()
    monkeypatch.setattr(instruction_service, "settings", SimpleNamespace(data_dir=data_dir))
    monkeypatch.setattr(settings, "mcp_config_path", str(data_dir / "mcp.json"))
