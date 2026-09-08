"""Application configuration and filesystem locations."""
# 文件职责：负责应用配置加载与校验中的 __init__ 子模块。
# 逻辑关系：上层通过 config/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from .settings import PROJECT_ROOT, Settings, settings

# 变量说明：__all__ 表示当前步骤使用的 __all__ 值。
__all__ = ["PROJECT_ROOT", "Settings", "settings"]
