"""Conversation turn staging and terminal delivery."""
# 文件职责：负责会话交付与清理中的 __init__ 子模块。
# 逻辑关系：上层通过 sessions/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
