"""Late-bound compatibility dependencies for the run domain."""
# 文件职责：负责运行创建、恢复、流式传输和生命周期中的 dependencies 子模块。
# 逻辑关系：上层通过 runs/dependencies.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from typing import Any

from src.model.gateway import ProviderConfig


# 函数职责：构建 model_call 对应的数据或流程。
# 参数关系：config 表示当前生效的配置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def build_model_call(config: ProviderConfig) -> Any:
    # Imported at call time so existing extensions that replace
    # src.runs.service.build_model_call keep controlling every run.
    from . import service as run_service

    return run_service.build_model_call(config)
