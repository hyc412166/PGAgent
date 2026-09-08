"""Structured evidence tools and durable workflow state."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 __init__ 子模块。
# 逻辑关系：上层通过 coding/evidence/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from .state import WorkflowEvidenceState
from .tools import record_debug_evidence, record_review_finding

# 变量说明：__all__ 表示当前步骤使用的 __all__ 值。
__all__ = ["WorkflowEvidenceState", "record_debug_evidence", "record_review_finding"]
