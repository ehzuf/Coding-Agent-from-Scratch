"""
工具注册表

管理所有可用工具，提供：
  - tools 列表：供 Agent 初始化时使用
  - find_tool(name)：根据名称查找工具
  - to_api_tools()：转换为 Anthropic API 需要的格式
"""

from .base import Tool
from .get_current_time import GetCurrentTimeTool
from .bash import BashTool
from .read import ReadTool
from .write import WriteTool
from .edit import EditTool
from .glob import GlobTool
from .grep import GrepTool
from .agent_tool import AgentTool
from .send_message import SendMessageTool

# 所有内置工具的实例
BUILTIN_TOOLS: list[Tool] = [
    GetCurrentTimeTool(),
    BashTool(),
    ReadTool(),
    WriteTool(),
    EditTool(),
    GlobTool(),
    GrepTool(),
]


def get_tools() -> list[Tool]:
    """获取所有可用工具。"""
    return BUILTIN_TOOLS.copy()


def find_tool(name: str, tools: list[Tool] | None = None) -> Tool | None:
    """根据名称查找工具。"""
    tools = tools or BUILTIN_TOOLS
    for tool in tools:
        if tool.name == name:
            return tool
    return None


def to_api_tools(tools: list[Tool] | None = None) -> list[dict]:
    """
    转换为 Anthropic API 需要的 tools 参数格式。

    Returns:
        [{"name": "...", "description": "...", "input_schema": {...}}, ...]
    """
    tools = tools or BUILTIN_TOOLS
    return [tool.to_api_format() for tool in tools]


__all__ = ["Tool", "AgentTool", "SendMessageTool", "get_tools", "find_tool", "to_api_tools", "BUILTIN_TOOLS"]
