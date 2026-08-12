from __future__ import annotations

import asyncio
import json

import pytest

from app.runtime.context_service import (
    ArtifactRef,
    CompactionGuard,
    ContextAssembler,
    ContextEpoch,
    ContextSnapshot,
    InMemoryArtifactStore,
    SemanticCompactor,
    VersionConflictError,
    compact_with_compare_and_swap,
    micro_compact_messages,
    parse_structured_summary,
)
from app.runtime.context import estimate_tokens


def test_micro_compact_stores_old_tool_output_and_keeps_latest() -> None:
    store = InMemoryArtifactStore()
    messages = [
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "one"}]},
        {"role": "tool", "tool_call_id": "one", "content": "x" * 10_000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "two"}]},
        {"role": "tool", "tool_call_id": "two", "content": "y" * 10_000},
    ]
    result = micro_compact_messages(messages, artifact_store=store, keep_recent_tool_results=1)
    assert result.compacted_count == 1
    assert result.artifact_refs
    assert "artifact:" in result.messages[2]["content"]
    assert result.messages[-1]["content"] == "y" * 10_000
    assert store.get(result.artifact_refs[0].artifact_id)


def test_summary_parser_accepts_fenced_json_and_normalizes_fields() -> None:
    summary = parse_structured_summary(
        "```json\n{" + '"objective":"ship","facts":"one","tool_state":{"ok":true}' + "}\n```",
        strict=True,
    )
    assert summary["objective"] == "ship"
    assert summary["facts"] == ["one"]
    assert summary["tool_state"] == {"ok": True}
    assert summary["pending_work"] == []


def test_assembler_separates_stable_prefix_and_dynamic_suffix_and_has_stable_key() -> None:
    assembler = ContextAssembler(max_tokens=2_000, output_reserve_tokens=100, safety_buffer_tokens=100)
    stable = assembler.stable_prefix(system_rules="safe", workspace_rules="repo")
    first = assembler.assemble(stable_prefix=stable, recent_messages=[{"role": "user", "content": "one"}])
    second = assembler.assemble(stable_prefix=stable, recent_messages=[{"role": "user", "content": "two"}])
    assert [item["content"] for item in first.stable_prefix] == ["## System rules\nsafe", "## Workspace rules\nrepo"]
    assert first.dynamic_suffix[-1]["content"] == "one"
    assert second.dynamic_suffix[-1]["content"] == "two"
    assert first.cache_key == second.cache_key
    assert first.cache_breakpoints == (len(first.stable_prefix),)


def test_semantic_compactor_defaults_to_current_model_and_returns_epoch() -> None:
    calls: list[dict] = []

    async def model_call(**kwargs):
        calls.append(kwargs)
        return {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "objective": "ship feature",
                        "completed_work": ["implemented"],
                        "pending_work": ["test"],
                    })
                }
            }],
        }

    result = asyncio.run(
        SemanticCompactor(model_call=model_call, retain_tokens=100).compact(
            [{"role": "user", "content": "ship feature"}, {"role": "assistant", "content": "implemented"}],
            session_id="s1",
            sequence=2,
        )
    )
    assert calls and calls[0]["tools"] == []
    assert calls[0]["mode"] == "compaction"
    assert result.used_model is True
    assert result.epoch is not None
    assert result.epoch.session_id == "s1"
    assert result.summary["pending_work"] == ["test"]
    # This tiny transcript is already smaller than the semantic summary, so
    # the no-progress guard keeps the active epoch lossless.
    assert result.after_tokens >= result.before_tokens
    assert result.ineffective is True


def test_semantic_compactor_uses_local_fallback_when_model_is_unavailable() -> None:
    result = asyncio.run(
        SemanticCompactor(retain_tokens=20).compact(
            [{"role": "user", "content": "do this"}, {"role": "tool", "content": "failed to read"}],
        )
    )
    assert result.fallback is True
    assert result.summary["objective"] == "do this"
    assert result.summary["errors"]


def test_compaction_guard_stops_after_ineffective_attempts() -> None:
    guard = CompactionGuard(max_attempts=2, min_reduction_ratio=0.1)
    assert guard.can_attempt()
    effective, _ = guard.evaluate(100, 100)
    assert not effective


def test_epoch_and_snapshot_are_round_trippable() -> None:
    epoch = ContextEpoch(
        session_id="s",
        end_sequence=8,
        summary={"objective": "x"},
        retained_messages=[{"role": "user", "content": "x"}],
        artifact_refs=[ArtifactRef(artifact_id="a")],
    )
    restored = ContextEpoch.from_dict(epoch.to_dict())
    snapshot = ContextSnapshot.from_epoch(restored)
    assert snapshot.to_dict()["session_id"] == "s"
    assert snapshot.messages == [{"role": "user", "content": "x"}]
    assert snapshot.artifact_refs[0].artifact_id == "a"


def test_compare_and_swap_rejects_new_transcript_version() -> None:
    async def run() -> None:
        compactor = SemanticCompactor(retain_tokens=20)
        with pytest.raises(VersionConflictError):
            await compact_with_compare_and_swap(
                compactor,
                [{"role": "user", "content": "x"}],
                expected_version=1,
                current_version=lambda: 2,
                commit=lambda _result: None,
            )

    asyncio.run(run())
