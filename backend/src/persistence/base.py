"""Shared SQLAlchemy base types and identity helpers."""
# 文件职责：负责数据库模型、默认数据与事务访问中的 base 子模块。
# 逻辑关系：上层通过 persistence/base.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# 函数职责：完成 utcnow 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 函数职责：完成 new_id 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def new_id() -> str:
    return str(uuid.uuid4())


# 类职责：定义 Base 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class Base(DeclarativeBase):
    pass


# 类职责：定义 TimestampMixin 在本领域中的数据与行为。
class TimestampMixin:
    # 变量说明：created_at 表示创建时间。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # 变量说明：updated_at 表示最近更新时间。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
