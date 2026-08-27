from __future__ import annotations

import asyncio
import json

from app.runtime.context_service import FilesystemArtifactStore, ToolOutputBudgeter
from app.services.artifact_service import ArtifactToolStore
from app.tools.registry import create_default_registry


def test_artifact_reader_returns_bounded_character_pages(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path / "session-one")
    ref = store.put("甲乙丙丁戊己庚辛", kind="tool_output")
    reader = ArtifactToolStore(store, max_page_chars=4)

    first = reader.read(ref.artifact_id, offset=0, limit=3)
    second = reader.read(ref.artifact_id, offset=3, limit=3)

    assert first.ok and first.content == "甲乙丙"
    assert first.metadata == {
        "artifact_id": ref.artifact_id,
        "offset": 0,
        "next_offset": 3,
        "total_chars": 8,
        "eof": False,
    }
    assert second.ok and second.content == "丁戊己"
    assert second.metadata["next_offset"] == 6


def test_artifact_reader_cannot_cross_session_store(tmp_path) -> None:
    first_store = FilesystemArtifactStore(tmp_path / "session-one")
    second_store = FilesystemArtifactStore(tmp_path / "session-two")
    ref = first_store.put("session one secret")

    result = ArtifactToolStore(second_store).read(ref.artifact_id)

    assert result.ok is False
    assert result.error_code == "artifact_not_found"
    assert "session one secret" not in result.content


def test_artifact_page_stays_below_tool_externalization_budget_after_json_escaping(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path / "session")
    ref = store.put("\x00" * 24_000)

    result = ArtifactToolStore(store).read(ref.artifact_id, limit=24_000)
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.ok
    assert len(serialized) < 30_000
    assert result.metadata["next_offset"] is not None
    prepared = ToolOutputBudgeter(store).prepare(
        tool_call_id="read-artifact-1",
        tool_name="read_artifact",
        output=serialized,
    )
    assert prepared.artifact_ref is None


def test_read_artifact_is_a_parallel_read_only_registry_tool(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path / "session")
    ref = store.put("artifact body")
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read_artifact"],
        artifact_store=ArtifactToolStore(store),
    )

    result = asyncio.run(registry.execute_async(
        "read_artifact",
        {"artifact_id": ref.artifact_id, "offset": 0, "limit": 100},
    ))

    assert result.ok and result.content == "artifact body"
    assert registry.enabled_tool_names == ("read_artifact",)
    assert registry.can_execute_batch_in_parallel(["read_artifact", "read_artifact"])
