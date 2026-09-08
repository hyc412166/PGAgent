"""Dependency-free attachment capability identifiers."""
# 文件职责：负责附件上传、元数据与文件存储中的 contracts 子模块。
# 逻辑关系：上层通过 attachments/contracts.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

# 变量说明：ATTACHMENT_TOOL_NAMES 表示当前流程使用的 ATTACHMENT_TOOL_NAMES 集合。
ATTACHMENT_TOOL_NAMES = (
    "list_attachments",
    "attachment_info",
    "read_attachment",
    "inspect_pdf",
    "render_pdf_page",
)
