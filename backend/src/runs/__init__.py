"""Run coordination, configuration, delegation, event streaming, and recovery.

Import concrete responsibilities from their modules, for example
``src.runs.lifecycle`` or ``src.runs.stream``. Keeping this package initializer
side-effect free prevents the context and run domains from loading each other
during package discovery.
"""
# 文件职责：负责运行创建、恢复、流式传输和生命周期中的 __init__ 子模块。
# 逻辑关系：上层通过 runs/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
