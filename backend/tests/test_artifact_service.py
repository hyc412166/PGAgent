"""验证会话产物存储与分页读取服务，包括会话隔离、字符预算和工具注册属性。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio
import json

from src.context.assembly import FilesystemArtifactStore, ToolOutputBudgeter
from src.artifacts.storage import ArtifactToolStore
from src.tools.registry import create_default_registry


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_artifact_reader_returns_bounded_character_pages 精确标识本用例的具体条件。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_artifact_reader_cannot_cross_session_store 精确标识本用例的具体条件。
def test_artifact_reader_cannot_cross_session_store(tmp_path) -> None:
    first_store = FilesystemArtifactStore(tmp_path / "session-one")
    second_store = FilesystemArtifactStore(tmp_path / "session-two")
    ref = first_store.put("session one secret")

    result = ArtifactToolStore(second_store).read(ref.artifact_id)

    assert result.ok is False
    assert result.error_code == "artifact_not_found"
    assert "session one secret" not in result.content


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_artifact_page_stays_below_tool_externalization_budget_after_json_escaping 精确标识本用例的具体条件。
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


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_read_artifact_is_a_parallel_read_only_registry_tool 精确标识本用例的具体条件。
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
