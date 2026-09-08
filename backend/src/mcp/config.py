"""Validated PGAgent MCP server configuration."""
# 文件职责：负责MCP 外部工具接入中的 config 子模块。
# 逻辑关系：上层通过 mcp/config.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# 变量说明：_ENV_REFERENCE 表示当前步骤使用的 _ENV_REFERENCE 值。
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# 类职责：定义 McpServerConfig 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class McpServerConfig(BaseModel):
    """One stdio or Streamable HTTP MCP server."""

    # 变量说明：model_config 表示当前步骤使用的 model_config 值。
    model_config = ConfigDict(extra="forbid")

    # 变量说明：command 表示当前步骤使用的 command 值。
    command: str | None = None
    # 变量说明：args 表示当前流程使用的 args 集合。
    args: list[str] = Field(default_factory=list)
    # 变量说明：env 表示当前步骤使用的 env 值。
    env: dict[str, str] = Field(default_factory=dict)
    # 变量说明：cwd 表示当前步骤使用的 cwd 值。
    cwd: str | None = None
    # 变量说明：url 表示当前步骤使用的 url 值。
    url: str | None = None
    # 变量说明：headers 表示当前流程使用的 headers 集合。
    headers: dict[str, str] = Field(default_factory=dict)
    # 变量说明：enabled 表示当前步骤使用的 enabled 值。
    enabled: bool = True
    # 变量说明：required 表示当前步骤使用的 required 值。
    required: bool = False
    # 变量说明：startup_timeout_sec 表示当前步骤使用的 startup_timeout_sec 值。
    startup_timeout_sec: float = Field(default=30.0, gt=0, le=300)
    # 变量说明：tool_timeout_sec 表示当前步骤使用的 tool_timeout_sec 值。
    tool_timeout_sec: float = Field(default=300.0, gt=0, le=1800)
    # 变量说明：enabled_tools 表示当前流程使用的 enabled_tools 集合。
    enabled_tools: list[str] | None = None
    # 变量说明：disabled_tools 表示当前流程使用的 disabled_tools 集合。
    disabled_tools: list[str] = Field(default_factory=list)
    # 变量说明：supports_parallel_tool_calls 表示当前流程使用的 supports_parallel_tool_calls 集合。
    supports_parallel_tool_calls: bool = False

    # 函数职责：校验 transport 对应的数据或流程。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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

    # 函数职责：完成 transport 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def transport(self) -> Literal["stdio", "streamable_http"]:
        return "stdio" if self.command else "streamable_http"

    # 函数职责：完成 allows_tool 对应的业务处理。
    # 参数关系：tool_name 表示当前步骤使用的 tool_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def allows_tool(self, tool_name: str) -> bool:
        if self.enabled_tools is not None and tool_name not in self.enabled_tools:
            return False
        return tool_name not in self.disabled_tools


# 类职责：定义 McpConfig 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class McpConfig(BaseModel):
    # 变量说明：model_config 表示当前步骤使用的 model_config 值。
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # 变量说明：servers 表示当前流程使用的 servers 集合。
    servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")


# 函数职责：校验 mcp_server_names 对应的数据或流程。
# 参数关系：config 表示当前生效的配置；names 表示当前流程使用的 names 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def validate_mcp_server_names(config: McpConfig, names: list[str]) -> list[str]:
    """Return a stable session selection or reject unavailable servers."""

    # 变量说明：selected 表示当前步骤使用的 selected 值。
    selected = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    # 变量说明：unavailable 表示当前步骤使用的 unavailable 值。
    unavailable = [
        name for name in selected
        if name not in config.servers or not config.servers[name].enabled
    ]
    if unavailable:
        raise ValueError(f"MCP servers are missing or disabled: {', '.join(unavailable)}")
    return selected


# 函数职责：完成 expand_environment 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _expand_environment(value: object) -> object:
    if isinstance(value, str):
        # 函数职责：完成 replace 对应的业务处理。
        # 参数关系：match 表示当前步骤使用的 match 值。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def replace(match: re.Match[str]) -> str:
            # 变量说明：name 表示当前对象名称。
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


# 函数职责：加载 mcp_config 对应的数据或流程。
# 参数关系：path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def load_mcp_config(path: Path) -> McpConfig:
    """Read one explicit application-owned MCP configuration file."""

    if not path.exists():
        return McpConfig()
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("MCP configuration root must be a JSON object")
    # 变量说明：server_key 表示当前步骤使用的 server_key 值。
    server_key = "mcpServers" if "mcpServers" in payload else "servers"
    # 变量说明：raw_servers 表示当前流程使用的 raw_servers 集合。
    raw_servers = payload.get(server_key, {})
    if not isinstance(raw_servers, dict):
        raise ValueError("MCP servers must be a JSON object")
    # 变量说明：expanded_servers 表示当前流程使用的 expanded_servers 集合。
    expanded_servers = {
        name: _expand_environment(server)
        if isinstance(server, dict) and server.get("enabled", True) is not False
        else server
        for name, server in raw_servers.items()
    }
    # 变量说明：expanded 表示当前步骤使用的 expanded 值。
    expanded = {**payload, server_key: expanded_servers}
    # 变量说明：config 表示当前生效的配置。
    config = McpConfig.model_validate(expanded)
    # 变量说明：invalid_names 表示当前流程使用的 invalid_names 集合。
    invalid_names = [name for name in config.servers if not name.strip()]
    if invalid_names:
        raise ValueError("MCP server names must not be empty")
    return config


# 函数职责：加载 mcp_config_source 对应的数据或流程。
# 参数关系：path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def load_mcp_config_source(path: Path) -> McpConfig:
    """Read the persisted values without expanding environment references."""

    if not path.exists():
        return McpConfig()
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("MCP configuration root must be a JSON object")
    # 变量说明：config 表示当前生效的配置。
    config = McpConfig.model_validate(payload)
    if any(not name.strip() for name in config.servers):
        raise ValueError("MCP server names must not be empty")
    return config


# 函数职责：保存 mcp_config 对应的数据或流程。
# 参数关系：path 表示当前文件或目录路径；config 表示当前生效的配置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def save_mcp_config(path: Path, config: McpConfig) -> None:
    """Persist one validated MCP configuration with an atomic file replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    # 变量说明：temporary 表示当前步骤使用的 temporary 值。
    temporary = path.with_name(f".{path.name}.tmp")
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload = config.model_dump(mode="json", by_alias=True, exclude_none=True)
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
