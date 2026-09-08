"""Session-scoped Model Context Protocol client runtime."""
# 文件职责：负责MCP 外部工具接入中的 __init__ 子模块。
# 逻辑关系：上层通过 mcp/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from .integration import attach_mcp_tools
from .runtime import mcp_runtime_pool

# 变量说明：__all__ 表示当前步骤使用的 __all__ 值。
__all__ = ["attach_mcp_tools", "mcp_runtime_pool"]
