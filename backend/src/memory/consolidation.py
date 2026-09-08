"""Phase-2 global consolidation prompt and operation parser."""
# 文件职责：负责长期记忆提取、合并、检索与偏好中的 consolidation 子模块。
# 逻辑关系：上层通过 memory/consolidation.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence


# 变量说明：MEMORY_KINDS 表示当前流程使用的 MEMORY_KINDS 集合。
MEMORY_KINDS = frozenset({"user", "feedback", "project", "reference"})


# 函数职责：完成 consolidation_prompt 对应的业务处理。
# 参数关系：rollout_outputs 表示当前流程使用的 rollout_outputs 集合；existing_memories 表示当前流程使用的 existing_memories 集合；existing_skills 表示当前流程使用的 existing_skills 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：解析 consolidation 对应的数据或流程。
# 参数关系：text 表示当前步骤使用的 text 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def parse_consolidation(text: str) -> dict[str, Any] | None:
    # 变量说明：raw 表示当前步骤使用的 raw 值。
    raw = str(text or "").strip()
    if raw.startswith("```"):
        # 变量说明：raw 表示当前步骤使用的 raw 值。
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S)
    try:
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    # 变量说明：upserts 表示当前流程使用的 upserts 集合。
    upserts: list[dict[str, Any]] = []
    for item in payload.get("upserts", [])[:100]:
        if not isinstance(item, Mapping):
            continue
        # 变量说明：name 表示当前对象名称。
        name = str(item.get("name") or "").strip()[:200]
        # 变量说明：content 表示待处理或返回的正文内容。
        content = str(item.get("content") or "").strip()[:20_000]
        # 变量说明：memory_type 表示当前步骤使用的 memory_type 值。
        memory_type = str(item.get("memory_type") or "project").lower()
        # 变量说明：scope 表示当前步骤使用的 scope 值。
        scope = str(item.get("scope") or "workspace").lower()
        # 变量说明：sources 表示当前流程使用的 sources 集合。
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
    # 变量说明：skills 表示当前流程使用的 skills 集合。
    skills: list[dict[str, Any]] = []
    for item in payload.get("skill_upserts", [])[:30]:
        if not isinstance(item, Mapping):
            continue
        # 变量说明：sources 表示当前流程使用的 sources 集合。
        sources = list(dict.fromkeys(
            str(value).strip() for value in item.get("source_rollout_ids", []) if str(value).strip()
        ))[:50]
        # 变量说明：name 表示当前对象名称。
        name = str(item.get("name") or "").strip()[:160]
        # 变量说明：content 表示待处理或返回的正文内容。
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
    # 变量说明：selected 表示当前步骤使用的 selected 值。
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
