"""Small real stdio MCP server used by the integration tests."""

import os

from mcp.server import MCPServer
from mcp.types import ToolAnnotations


server = MCPServer("pgagent-test-server", version="1.0")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(text: str) -> str:
    """Return text without changing state."""

    return f"echo:{text}"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def cwd() -> str:
    """Return the stdio server process working directory."""

    return os.getcwd()


@server.tool()
def remember(value: str) -> str:
    """Represent a state-changing external operation."""

    return f"remembered:{value}"


if __name__ == "__main__":
    server.run("stdio")
