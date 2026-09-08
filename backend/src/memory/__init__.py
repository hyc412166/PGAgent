"""Two-phase persistent memory with model routing and usage feedback.

Boundaries:
- :mod:`extraction` filters one accepted rollout and parses Phase-1 evidence.
- :mod:`consolidation` defines and validates Phase-2 handbook operations.
- :mod:`pipeline` owns durable asynchronous model jobs.
- :mod:`protocol` owns provider index/citation framing.
- :mod:`repository` owns citation usage and index persistence queries.
- :mod:`service` remains the compatibility facade for memory CRUD/tools and
  deterministic Markdown projection.
"""
# 文件职责：负责长期记忆提取、合并、检索与偏好中的 __init__ 子模块。
# 逻辑关系：上层通过 memory/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
