"""Conversation-private attachments.

Keep this package initializer dependency-free: persistence imports the tools
package while attachment storage itself uses persistence.
"""
# 文件职责：负责附件上传、元数据与文件存储中的 __init__ 子模块。
# 逻辑关系：上层通过 attachments/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from .contracts import ATTACHMENT_TOOL_NAMES

# 变量说明：__all__ 表示当前步骤使用的 __all__ 值。
__all__ = ["ATTACHMENT_TOOL_NAMES"]
