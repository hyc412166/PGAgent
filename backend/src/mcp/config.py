"""Validated PGAgent MCP server configuration."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpServerConfig(BaseModel):
    """One stdio or Streamable HTTP MCP server."""

    model_config = ConfigDict(extra="forbid")

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    required: bool = False
    startup_timeout_sec: float = Field(default=30.0, gt=0, le=300)
    tool_timeout_sec: float = Field(default=300.0, gt=0, le=1800)
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] = Field(default_factory=list)
    supports_parallel_tool_calls: bool = False

    @model_validator(mode="after")
    def validate_transport(self) -> "McpServerConfig":
        if bool(self.command) == bool(self.url):
            raise ValueError("configure exactly one of command or url")
        if (
            self.enabled
            and self.url
            and _ENV_REFERENCE.fullmatch(self.url) is None
            and not self.url.lower().startswith(("http://", "https://"))
        ):
            raise ValueError("MCP url must use http or https")
        if self.url and (self.args or self.env or self.cwd):
            raise ValueError("args, env and cwd are only valid for stdio servers")
        if self.command and self.headers:
            raise ValueError("headers are only valid for Streamable HTTP servers")
        return self

    @property
    def transport(self) -> Literal["stdio", "streamable_http"]:
        return "stdio" if self.command else "streamable_http"

    def allows_tool(self, tool_name: str) -> bool:
        if self.enabled_tools is not None and tool_name not in self.enabled_tools:
            return False
        return tool_name not in self.disabled_tools


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")


def validate_mcp_server_names(config: McpConfig, names: list[str]) -> list[str]:
    """Return a stable session selection or reject unavailable servers."""

    selected = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    unavailable = [
        name for name in selected
        if name not in config.servers or not config.servers[name].enabled
    ]
    if unavailable:
        raise ValueError(f"MCP servers are missing or disabled: {', '.join(unavailable)}")
    return selected


def _expand_environment(value: object) -> object:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ValueError(f"environment variable {name!r} is not set")
            return os.environ[name]

        return _ENV_REFERENCE.sub(replace, value)
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_environment(item) for key, item in value.items()}
    return value


def load_mcp_config(path: Path) -> McpConfig:
    """Read one explicit application-owned MCP configuration file."""

    if not path.exists():
        return McpConfig()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("MCP configuration root must be a JSON object")
    server_key = "mcpServers" if "mcpServers" in payload else "servers"
    raw_servers = payload.get(server_key, {})
    if not isinstance(raw_servers, dict):
        raise ValueError("MCP servers must be a JSON object")
    expanded_servers = {
        name: _expand_environment(server)
        if isinstance(server, dict) and server.get("enabled", True) is not False
        else server
        for name, server in raw_servers.items()
    }
    expanded = {**payload, server_key: expanded_servers}
    config = McpConfig.model_validate(expanded)
    invalid_names = [name for name in config.servers if not name.strip()]
    if invalid_names:
        raise ValueError("MCP server names must not be empty")
    return config


def load_mcp_config_source(path: Path) -> McpConfig:
    """Read the persisted values without expanding environment references."""

    if not path.exists():
        return McpConfig()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("MCP configuration root must be a JSON object")
    config = McpConfig.model_validate(payload)
    if any(not name.strip() for name in config.servers):
        raise ValueError("MCP server names must not be empty")
    return config


def save_mcp_config(path: Path, config: McpConfig) -> None:
    """Persist one validated MCP configuration with an atomic file replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    payload = config.model_dump(mode="json", by_alias=True, exclude_none=True)
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
