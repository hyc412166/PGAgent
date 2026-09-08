"""Session-bound, paginated access to runtime artifacts."""
# 文件职责：负责运行产物存储中的 storage 子模块。
# 逻辑关系：上层通过 artifacts/storage.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import json

from src.context.assembly import ArtifactStore, DEFAULT_TOOL_OUTPUT_MAX_CHARS
from src.tools.types import ToolResult


# 变量说明：DEFAULT_ARTIFACT_PAGE_CHARS 表示当前流程使用的 DEFAULT_ARTIFACT_PAGE_CHARS 集合。
DEFAULT_ARTIFACT_PAGE_CHARS = 20_000
# 变量说明：MAX_ARTIFACT_PAGE_CHARS 表示当前流程使用的 MAX_ARTIFACT_PAGE_CHARS 集合。
MAX_ARTIFACT_PAGE_CHARS = 24_000
# 变量说明：MAX_SERIALIZED_ARTIFACT_RESULT_CHARS 表示当前流程使用的 MAX_SERIALIZED_ARTIFACT_RESULT_CHARS 集合。
MAX_SERIALIZED_ARTIFACT_RESULT_CHARS = DEFAULT_TOOL_OUTPUT_MAX_CHARS - 1_000


# 类职责：封装 ArtifactToolStore 的持久化访问。
class ArtifactToolStore:
    """Expose only artifacts addressable by one runtime's bound store."""

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：artifact_store 表示当前步骤使用的 artifact_store 值；max_page_chars 表示当前流程使用的 max_page_chars 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self, artifact_store: ArtifactStore, *, max_page_chars: int = MAX_ARTIFACT_PAGE_CHARS) -> None:
        # 变量说明：artifact_store 表示当前步骤使用的 artifact_store 值。
        self.artifact_store = artifact_store
        # 变量说明：max_page_chars 表示当前流程使用的 max_page_chars 集合。
        self.max_page_chars = max(1, int(max_page_chars))

    # 函数职责：完成 read 对应的业务处理。
    # 参数关系：artifact_id 表示artifact 对象的唯一标识；offset 表示当前步骤使用的 offset 值；limit 表示当前步骤使用的 limit 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def read(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int = DEFAULT_ARTIFACT_PAGE_CHARS,
    ) -> ToolResult:
        try:
            # 变量说明：start 表示当前步骤使用的 start 值。
            start = int(offset)
            # 变量说明：requested 表示当前步骤使用的 requested 值。
            requested = int(limit)
        except (TypeError, ValueError):
            return ToolResult(
                "read_artifact",
                False,
                "offset and limit must be integers",
                error_code="invalid_arguments",
            )
        if start < 0 or requested < 1 or requested > self.max_page_chars:
            return ToolResult(
                "read_artifact",
                False,
                f"offset must be non-negative and limit must be between 1 and {self.max_page_chars}",
                error_code="invalid_arguments",
            )
        try:
            # 变量说明：payload 表示跨层传递的数据载荷。
            payload = self.artifact_store.get(str(artifact_id or ""))
        except (OSError, ValueError):
            # 变量说明：payload 表示跨层传递的数据载荷。
            payload = None
        if payload is None:
            return ToolResult(
                "read_artifact",
                False,
                "Artifact is not available in this session",
                error_code="artifact_not_found",
            )
        # 变量说明：text 表示当前步骤使用的 text 值。
        text = payload.decode("utf-8", errors="replace")
        # 变量说明：maximum_end 表示当前步骤使用的 maximum_end 值。
        maximum_end = min(len(text), start + requested)

        # 函数职责：完成 page 对应的业务处理。
        # 参数关系：end 表示当前步骤使用的 end 值。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def page(end: int) -> ToolResult:
            # 变量说明：chunk 表示当前步骤使用的 chunk 值。
            chunk = text[start:end]
            # 变量说明：eof 表示当前步骤使用的 eof 值。
            eof = end >= len(text)
            return ToolResult(
                "read_artifact",
                True,
                chunk,
                metadata={
                    "artifact_id": str(artifact_id),
                    "offset": start,
                    "next_offset": None if eof else end,
                    "total_chars": len(text),
                    "eof": eof,
                },
            )

        # 变量说明：candidate 表示当前步骤使用的 candidate 值。
        candidate = page(maximum_end)
        if len(json.dumps(candidate.to_dict(), ensure_ascii=False)) < MAX_SERIALIZED_ARTIFACT_RESULT_CHARS:
            return candidate
        # 变量说明：low 表示当前步骤使用的 low 值。
        low = min(start + 1, maximum_end)
        # 变量说明：high 表示当前步骤使用的 high 值。
        high = maximum_end
        # 变量说明：best 表示当前步骤使用的 best 值。
        best = page(start)
        while low <= high:
            # 变量说明：middle 表示当前步骤使用的 middle 值。
            middle = (low + high) // 2
            # 变量说明：current 表示当前步骤使用的 current 值。
            current = page(middle)
            if len(json.dumps(current.to_dict(), ensure_ascii=False)) < MAX_SERIALIZED_ARTIFACT_RESULT_CHARS:
                # 变量说明：best 表示当前步骤使用的 best 值。
                best = current
                # 变量说明：low 表示当前步骤使用的 low 值。
                low = middle + 1
            else:
                # 变量说明：high 表示当前步骤使用的 high 值。
                high = middle - 1
        return best
