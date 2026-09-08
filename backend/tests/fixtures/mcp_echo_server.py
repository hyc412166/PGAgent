"""Small real stdio MCP server used by the integration tests."""

# 文件职责：提供 MCP 运行时测试专用的 stdio 回显服务，验证跨进程工具发现、调用、工作目录和副作用标记。
# 变量约定：server 是协议服务实例，text/value 是调用输入，函数返回值是运行时断言的可观察输出。

import os

from mcp.server import MCPServer
from mcp.types import ToolAnnotations


# 测试用 stdio MCP 服务：提供只读回显/工作目录查询和一个状态变更工具。
server = MCPServer("pgagent-test-server", version="1.0")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
# 辅助函数：echo 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def echo(text: str) -> str:
    """Return text without changing state."""

    # text 是调用方传入的待回显文本；返回值用于验证工具适配链路。
    return f"echo:{text}"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
# 辅助函数：cwd 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def cwd() -> str:
    """Return the stdio server process working directory."""

    # 当前目录由 MCP 子进程决定，用于确认进程工作目录传递正确。
    return os.getcwd()


@server.tool()
# 辅助函数：remember 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def remember(value: str) -> str:
    """Represent a state-changing external operation."""

    # value 代表外部状态写入内容；测试只模拟返回确认文本，不持久化数据。
    return f"remembered:{value}"


if __name__ == "__main__":
    server.run("stdio")
