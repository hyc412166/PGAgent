"""Persistent-memory preferences shared by API and runtime layers."""
# 文件职责：负责长期记忆提取、合并、检索与偏好中的 preferences 子模块。
# 逻辑关系：上层通过 memory/preferences.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from sqlalchemy.orm import Session

from src.persistence.models import MemorySettings


# 变量说明：GLOBAL_MEMORY_SETTINGS_ID 表示GLOBAL_MEMORY_SETTINGS 对象的唯一标识。
GLOBAL_MEMORY_SETTINGS_ID = "global"


# 函数职责：读取 memory_settings 对应的数据或流程。
# 参数关系：db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def get_memory_settings(db: Session) -> MemorySettings:
    """Return the seeded process-wide memory settings row."""

    # 变量说明：settings 表示应用设置集合。
    settings = db.get(MemorySettings, GLOBAL_MEMORY_SETTINGS_ID)
    if settings is None:
        raise RuntimeError("Global memory settings have not been initialized")
    return settings


# 函数职责：完成 memories_enabled 对应的业务处理。
# 参数关系：db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def memories_enabled(db: Session) -> bool:
    return bool(get_memory_settings(db).enabled)
