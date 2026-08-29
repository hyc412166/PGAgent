"""Context windows, prompt assembly, compaction, and instruction discovery."""

from .assembly import (
    ArtifactRef, ArtifactStore, CompactionResult, ContextAssembler, ConversationCompactor,
    FilesystemArtifactStore, InMemoryArtifactStore, PreparedToolOutput, PromptLayout,
    ToolOutputBudgeter, ToolResultCompaction, atomic_message_groups,
    compact_tool_results_for_model, retain_recent_atomic_tail,
)
from .window import DEFAULT_COMPACT_THRESHOLD_TOKENS, DEFAULT_CONTEXT_LIMIT_TOKENS, ContextManager

__all__ = [
    "ArtifactRef", "ArtifactStore", "CompactionResult", "ContextAssembler", "ContextManager",
    "ConversationCompactor", "DEFAULT_COMPACT_THRESHOLD_TOKENS", "DEFAULT_CONTEXT_LIMIT_TOKENS",
    "FilesystemArtifactStore", "InMemoryArtifactStore", "PreparedToolOutput", "PromptLayout",
    "ToolOutputBudgeter", "ToolResultCompaction", "atomic_message_groups",
    "compact_tool_results_for_model", "retain_recent_atomic_tail",
]
