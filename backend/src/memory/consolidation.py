"""Phase-2 global consolidation prompt and operation parser."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence


MEMORY_KINDS = frozenset({"user", "feedback", "project", "reference"})


def consolidation_prompt(
    *,
    rollout_outputs: Sequence[Mapping[str, Any]],
    existing_memories: Sequence[Mapping[str, Any]],
    existing_skills: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are the Phase-2 persistent-memory consolidation agent. Maintain a compact, human-readable "
                "handbook from Stage-1 evidence. Merge duplicates, preserve cwd/project boundaries, resolve conflicts "
                "in favor of newer explicit user feedback or verified evidence, retain useful failures, and archive "
                "obsolete entries. Do not cluster on generic keywords alone. Usage is supporting evidence, never a "
                "reason to discard a new memory. Return JSON only with upserts, archive_memory_ids, and "
                "selected_rollout_ids. Every upsert must include source_rollout_ids. Promote a procedure to "
                "skill_upserts only when at least two distinct rollouts support the same reliable workflow. "
                "Do not invent evidence."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({
                "stage1_outputs": list(rollout_outputs),
                "existing_memory_handbook": list(existing_memories),
                "existing_memory_skills": list(existing_skills),
                "output_schema": {
                    "upserts": [{
                        "memory_id": "existing id or empty",
                        "name": "stable searchable name",
                        "memory_type": "user|feedback|project|reference",
                        "description": "one-line routing description",
                        "content": "consolidated handbook entry",
                        "tags": ["searchable", "keywords"],
                        "scope": "global|workspace",
                        "workspace_id": "id or empty for global",
                        "source_rollout_ids": ["id"],
                    }],
                    "archive_memory_ids": ["obsolete id"],
                    "skill_upserts": [{
                        "skill_id": "existing id or empty",
                        "name": "searchable-procedure-name",
                        "description": "when this procedure applies",
                        "content": "reusable ordered procedure",
                        "keywords": ["directly searchable term"],
                        "source_rollout_ids": ["at least two ids"],
                    }],
                    "archive_skill_ids": ["obsolete skill id"],
                    "selected_rollout_ids": ["processed id"],
                },
            }, ensure_ascii=False),
        },
    ]


def parse_consolidation(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    upserts: list[dict[str, Any]] = []
    for item in payload.get("upserts", [])[:100]:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()[:200]
        content = str(item.get("content") or "").strip()[:20_000]
        memory_type = str(item.get("memory_type") or "project").lower()
        scope = str(item.get("scope") or "workspace").lower()
        sources = list(dict.fromkeys(str(value).strip() for value in item.get("source_rollout_ids", []) if str(value).strip()))[:50]
        if not name or len(content) < 8 or memory_type not in MEMORY_KINDS or scope not in {"global", "workspace"} or not sources:
            continue
        upserts.append({
            "memory_id": str(item.get("memory_id") or "").strip(),
            "name": name,
            "memory_type": memory_type,
            "description": str(item.get("description") or "").strip()[:1_000],
            "content": content,
            "tags": list(dict.fromkeys(str(value).strip()[:100] for value in item.get("tags", []) if str(value).strip()))[:30],
            "scope": scope,
            "workspace_id": str(item.get("workspace_id") or "").strip() or None,
            "source_rollout_ids": sources,
        })
    skills: list[dict[str, Any]] = []
    for item in payload.get("skill_upserts", [])[:30]:
        if not isinstance(item, Mapping):
            continue
        sources = list(dict.fromkeys(
            str(value).strip() for value in item.get("source_rollout_ids", []) if str(value).strip()
        ))[:50]
        name = str(item.get("name") or "").strip()[:160]
        content = str(item.get("content") or "").strip()[:30_000]
        if not name or len(content) < 20 or len(sources) < 2:
            continue
        skills.append({
            "skill_id": str(item.get("skill_id") or "").strip(),
            "name": name,
            "description": str(item.get("description") or "").strip()[:1_000],
            "content": content,
            "keywords": list(dict.fromkeys(str(value).strip()[:100] for value in item.get("keywords", []) if str(value).strip()))[:30],
            "source_rollout_ids": sources,
        })
    selected = list(dict.fromkeys(str(value).strip() for value in payload.get("selected_rollout_ids", []) if str(value).strip()))[:200]
    if not upserts and not skills and not selected:
        return None
    return {
        "upserts": upserts,
        "archive_memory_ids": list(dict.fromkeys(str(value).strip() for value in payload.get("archive_memory_ids", []) if str(value).strip()))[:100],
        "skill_upserts": skills,
        "archive_skill_ids": list(dict.fromkeys(str(value).strip() for value in payload.get("archive_skill_ids", []) if str(value).strip()))[:100],
        "selected_rollout_ids": selected,
    }
