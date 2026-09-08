"""Relational persistence primitives, entities, and built-in defaults."""
# 文件职责：负责数据库模型、默认数据与事务访问中的 __init__ 子模块。
# 逻辑关系：上层通过 persistence/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from src.persistence.run_events import append_run_event

__all__ = ["append_run_event"]
