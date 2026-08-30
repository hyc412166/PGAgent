"""Local application settings for PGAgent."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PGAGENT_", env_file=PROJECT_ROOT / ".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "INFO"
    # Zero/None disables aggregate run limits. Loop safety is enforced by the
    # repeated-call and no-progress guards instead of an arbitrary task size.
    max_steps: int | None = 0
    max_tool_calls: int | None = 0
    max_identical_calls: int = 3
    no_progress_limit: int = 4
    model_timeout_seconds: float = 90.0
    max_run_seconds: float | None = 1_800.0
    context_limit_tokens: int = 200_000
    compact_threshold_tokens: int = 180_000
    completion_verification_max_attempts: int = 3
    mcp_config_path: str | None = None

    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def mcp_config_file(self) -> Path:
        if self.mcp_config_path:
            return Path(self.mcp_config_path).expanduser().resolve()
        return self.data_dir / "mcp.json"

settings = Settings()
