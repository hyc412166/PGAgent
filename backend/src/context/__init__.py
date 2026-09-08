"""Context windows, prompt assembly, compaction, and instruction discovery."""
# 文件职责：负责模型上下文组装、窗口预算与压缩中的 __init__ 子模块。
# 逻辑关系：上层通过 context/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from .assembly import (
    ArtifactRef, ArtifactStore, CompactionResult, ContextAssembler, ConversationCompactor,
    FilesystemArtifactStore, InMemoryArtifactStore, PreparedToolOutput, PromptLayout,
    ToolOutputBudgeter, ToolResultCompaction, atomic_message_groups,
    compact_tool_results_for_model, retain_recent_atomic_tail,
)
from .window import DEFAULT_COMPACT_THRESHOLD_TOKENS, DEFAULT_CONTEXT_LIMIT_TOKENS, ContextManager

# 变量说明：__all__ 表示当前步骤使用的 __all__ 值。
__all__ = [
    "ArtifactRef", "ArtifactStore", "CompactionResult", "ContextAssembler", "ContextManager",
    "ConversationCompactor", "DEFAULT_COMPACT_THRESHOLD_TOKENS", "DEFAULT_CONTEXT_LIMIT_TOKENS",
    "FilesystemArtifactStore", "InMemoryArtifactStore", "PreparedToolOutput", "PromptLayout",
    "ToolOutputBudgeter", "ToolResultCompaction", "atomic_message_groups",
    "compact_tool_results_for_model", "retain_recent_atomic_tail",
]
