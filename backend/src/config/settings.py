"""Local application settings for PGAgent."""
# 文件职责：负责应用配置加载与校验中的 settings 子模块。
# 逻辑关系：上层通过 config/settings.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# 变量说明：PROJECT_ROOT 表示当前步骤使用的 PROJECT_ROOT 值。
PROJECT_ROOT = Path(__file__).resolve().parents[3]


# 类职责：定义 Settings 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class Settings(BaseSettings):
    # 变量说明：model_config 表示当前步骤使用的 model_config 值。
    model_config = SettingsConfigDict(env_prefix="PGAGENT_", env_file=PROJECT_ROOT / ".env", extra="ignore")

    # 变量说明：host 表示当前步骤使用的 host 值。
    host: str = "127.0.0.1"
    # 变量说明：port 表示当前步骤使用的 port 值。
    port: int = 8765
    # 变量说明：log_level 表示当前步骤使用的 log_level 值。
    log_level: str = "INFO"
    # 诊断文件日志默认位于应用 data 目录，单独目录方便按策略清理。
    log_dir: str | None = None
    log_retention_days: int = Field(default=14, ge=1)
    log_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    # Zero/None disables aggregate step/call limits. Repetition and no-progress
    # heuristics are opt-in; durable task recovery is not cut off by default.
    # 变量说明：max_steps 表示当前流程使用的 max_steps 集合。
    max_steps: int | None = 0
    # 变量说明：max_tool_calls 表示当前流程使用的 max_tool_calls 集合。
    max_tool_calls: int | None = 0
    # 变量说明：max_identical_calls 表示当前流程使用的 max_identical_calls 集合。
    max_identical_calls: int = 0
    # 变量说明：no_progress_limit 表示当前步骤使用的 no_progress_limit 值。
    no_progress_limit: int = 0
    # 流式模型连续无事件的 idle 超时；整次任务默认没有墙钟上限。
    # 变量说明：model_timeout_seconds 表示当前流程使用的 model_timeout_seconds 集合。
    model_timeout_seconds: float = 300.0
    # 变量说明：max_run_seconds 表示当前流程使用的 max_run_seconds 集合。
    max_run_seconds: float | None = None
    # 变量说明：model_request_timeout_seconds 表示当前流程使用的 model_request_timeout_seconds 集合。
    model_request_timeout_seconds: float = 300.0
    # 变量说明：model_request_retries 表示当前流程使用的 model_request_retries 集合。
    model_request_retries: int = 5
    # 变量说明：model_stream_retries 表示当前流程使用的 model_stream_retries 集合。
    model_stream_retries: int = 5
    # 变量说明：delegated_wait_timeout_seconds 表示当前流程使用的 delegated_wait_timeout_seconds 集合。
    delegated_wait_timeout_seconds: float = 30.0
    # 变量说明：max_task_tokens 表示当前流程使用的 max_task_tokens 集合。
    max_task_tokens: int | None = None
    # 变量说明：context_limit_tokens 表示当前流程使用的 context_limit_tokens 集合。
    context_limit_tokens: int = 200_000
    # 变量说明：compact_threshold_tokens 表示当前流程使用的 compact_threshold_tokens 集合。
    compact_threshold_tokens: int = 180_000
    # 变量说明：completion_verification_max_attempts 表示当前流程使用的 completion_verification_max_attempts 集合。
    completion_verification_max_attempts: int = 3
    # 变量说明：mcp_config_path 表示mcp_config_path 对应的文件系统位置。
    mcp_config_path: str | None = None

    # 函数职责：完成 data_dir 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def resolved_log_dir(self) -> Path:
        return Path(self.log_dir).expanduser().resolve() if self.log_dir else self.data_dir / "logs"

    # 函数职责：完成 workspaces_dir 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    # 函数职责：完成 mcp_config_file 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def mcp_config_file(self) -> Path:
        if self.mcp_config_path:
            return Path(self.mcp_config_path).expanduser().resolve()
        return self.data_dir / "mcp.json"

# 变量说明：settings 表示应用设置集合。
settings = Settings()
