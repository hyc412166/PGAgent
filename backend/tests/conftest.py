"""配置后端测试的全局隔离环境。自动夹具把个人代理目录、应用数据目录和相关配置切换到临时目录，供所有测试模块共享。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.config import settings
from src.context import instructions as instruction_service


# 每个测试使用临时数据目录，避免真实用户目录、MCP 配置和测试之间互相污染。
@pytest.fixture(autouse=True)
# 测试夹具：isolate_personal_agents_home 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def isolate_personal_agents_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    # data_dir 保存本次测试的隔离文件；monkeypatch 将配置指向该目录并在测试后自动恢复。
    data_dir = tmp_path / "pgagent-data"
    data_dir.mkdir()
    # 运行时、附件和上下文服务共享这个单例设置对象；统一替换其数据根目录，防止测试写入正式 data。
    monkeypatch.setattr(type(settings), "data_dir", property(lambda _settings: data_dir))
    monkeypatch.setattr(instruction_service, "settings", SimpleNamespace(data_dir=data_dir))
    monkeypatch.setattr(settings, "mcp_config_path", str(data_dir / "mcp.json"))
