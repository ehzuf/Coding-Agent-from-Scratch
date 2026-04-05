"""
MCP Client —— Model Context Protocol 客户端

对应 reference 中的 services/mcp/client.ts + tools/MCPTool/

核心设计：
  - 连接 stdio / SSE 传输层的 MCP Server
  - 启动时获取工具列表（tools/list），转换为 Agent 可用的 Tool
  - MCPTool 将 Agent 的 tool_use 请求转发到 MCP Server
  - 配置从 ~/.coding-agent/settings.json 和 .coding-agent/settings.json 读取
  - 生命周期：连接 → 工具发现 → 代理调用 → 关闭

MCP 协议简述：
  - Client ↔ Server 通过 JSON-RPC 2.0 通信
  - Server 暴露 tools（工具）、resources（资源）、prompts（提示模板）
  - Client 调用 tools/list 发现工具，tools/call 执行工具
"""

import asyncio
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from agent.tools.base import Tool


# ============================================================================
# 配置
# ============================================================================

AGENT_HOME = os.path.expanduser("~/.coding-agent")


@dataclass
class MCPServerConfig:
    """单个 MCP Server 的配置。"""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    transport: str = "stdio"  # stdio 或 sse
    url: str | None = None  # SSE 模式的 URL


def load_mcp_config(cwd: str = ".") -> dict[str, MCPServerConfig]:
    """
    从配置文件加载 MCP Server 列表。

    优先级：项目配置 > 全局配置（合并，项目覆盖同名）。

    配置格式：
    {
      "mcpServers": {
        "server-name": {
          "type": "stdio",
          "command": "uvx",
          "args": ["mcp-server-xxx"],
          "env": {"KEY": "value"}
        }
      }
    }
    """
    servers: dict[str, MCPServerConfig] = {}

    # 全局配置
    global_config_path = os.path.join(AGENT_HOME, "settings.json")
    _load_servers_from_file(global_config_path, servers)

    # 项目配置（覆盖同名）
    project_config_path = os.path.join(cwd, ".coding-agent", "settings.json")
    _load_servers_from_file(project_config_path, servers)

    return servers


def _load_servers_from_file(
    path: str,
    servers: dict[str, MCPServerConfig],
) -> None:
    """从单个配置文件加载 MCP Server 配置。"""
    if not os.path.isfile(path):
        return
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        mcp_servers = data.get("mcpServers", {})
        for name, config in mcp_servers.items():
            if not isinstance(config, dict):
                continue
            transport = config.get("type", "stdio")
            servers[name] = MCPServerConfig(
                name=name,
                command=config.get("command", ""),
                args=config.get("args", []),
                env=config.get("env"),
                cwd=config.get("cwd"),
                transport=transport,
                url=config.get("url"),
            )
    except (json.JSONDecodeError, OSError):
        pass


# ============================================================================
# MCP 连接管理
# ============================================================================

@dataclass
class MCPConnection:
    """
    一个 MCP Server 的连接实例。

    管理到 MCP Server 的连接生命周期和工具发现。
    """
    config: MCPServerConfig
    tools: list["MCPProxyTool"] = field(default_factory=list)
    _session: object | None = None  # ClientSession
    _connected: bool = False
    _loop: asyncio.AbstractEventLoop | None = None  # 持久事件循环

    async def connect(self, loop: asyncio.AbstractEventLoop | None = None) -> bool:
        """
        连接到 MCP Server 并发现工具。

        Returns:
            是否连接成功
        """
        self._loop = loop
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp import stdio_client

            if self.config.transport == "stdio":
                if not self.config.command:
                    return False

                server_params = StdioServerParameters(
                    command=self.config.command,
                    args=self.config.args,
                    env=self.config.env,
                    cwd=self.config.cwd,
                )

                # 使用 stdio_client 上下文管理器连接
                self._stdio_cm = stdio_client(server_params)
                read_stream, write_stream = await self._stdio_cm.__aenter__()

                self._session_cm = ClientSession(read_stream, write_stream)
                session = await self._session_cm.__aenter__()
                self._session = session

                # 初始化
                await session.initialize()

                # 发现工具
                result = await session.list_tools()
                self.tools = []
                for tool in result.tools:
                    proxy = MCPProxyTool(
                        server_name=self.config.name,
                        tool_name=tool.name,
                        tool_description=tool.description or "",
                        tool_input_schema=tool.inputSchema or {},
                        session=session,
                        loop=loop,
                    )
                    self.tools.append(proxy)

                self._connected = True
                return True

            elif self.config.transport == "sse":
                if not self.config.url:
                    return False

                from mcp.client.sse import sse_client
                from mcp import ClientSession

                self._sse_cm = sse_client(self.config.url)
                read_stream, write_stream = await self._sse_cm.__aenter__()

                self._session_cm = ClientSession(read_stream, write_stream)
                session = await self._session_cm.__aenter__()
                self._session = session

                await session.initialize()

                result = await session.list_tools()
                self.tools = []
                for tool in result.tools:
                    proxy = MCPProxyTool(
                        server_name=self.config.name,
                        tool_name=tool.name,
                        tool_description=tool.description or "",
                        tool_input_schema=tool.inputSchema or {},
                        session=session,
                        loop=loop,
                    )
                    self.tools.append(proxy)

                self._connected = True
                return True

            return False

        except Exception as e:
            print(f"  [MCP] {self.config.name} 连接失败: {e}", file=sys.stderr)
            return False

    async def close(self) -> None:
        """关闭连接。"""
        try:
            if hasattr(self, "_session_cm") and self._session_cm:
                await self._session_cm.__aexit__(None, None, None)
            if hasattr(self, "_stdio_cm") and self._stdio_cm:
                await self._stdio_cm.__aexit__(None, None, None)
            if hasattr(self, "_sse_cm") and self._sse_cm:
                await self._sse_cm.__aexit__(None, None, None)
        except Exception:
            pass
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


# ============================================================================
# MCPProxyTool —— 将 MCP 工具代理为 Agent Tool
# ============================================================================

class MCPProxyTool(Tool):
    """
    MCP 工具代理。

    将 MCP Server 暴露的工具包装为 Agent 可用的 Tool。
    Agent 调用时，转发请求到 MCP Server。
    """

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        tool_description: str,
        tool_input_schema: dict,
        session: object,  # ClientSession
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        self._server_name = server_name
        self._tool_name = tool_name
        self._tool_description = tool_description
        self._tool_input_schema = tool_input_schema
        self._session = session
        self._loop = loop  # 持久事件循环（由 MCPManager 管理）

    @property
    def name(self) -> str:
        """工具名称：server_name__tool_name 格式，避免冲突。"""
        return f"mcp_{self._server_name}__{self._tool_name}"

    @property
    def description(self) -> str:
        return f"[MCP: {self._server_name}] {self._tool_description}"

    @property
    def input_schema(self) -> dict:
        return self._tool_input_schema

    def call(self, tool_input: dict) -> str:
        """
        调用 MCP Server 的工具。

        通过 MCPManager 的持久事件循环执行异步调用。
        """
        try:
            if self._loop and self._loop.is_running():
                # 使用持久事件循环（正常路径）
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(
                    self._call_async(tool_input), self._loop
                )
                return future.result(timeout=30)
            else:
                # 回退：直接运行（用于测试等场景）
                return asyncio.run(self._call_async(tool_input))
        except Exception as e:
            return f"MCP 工具调用失败: {e}"

    async def _call_async(self, tool_input: dict) -> str:
        """异步调用 MCP 工具。"""
        result = await self._session.call_tool(self._tool_name, tool_input)

        # 将结果转换为字符串
        parts = []
        for content in result.content:
            if hasattr(content, "text"):
                parts.append(content.text)
            elif hasattr(content, "data"):
                parts.append(f"[Binary data: {content.mimeType}]")
            else:
                parts.append(str(content))

        return "\n".join(parts) if parts else "(empty result)"

    def is_concurrency_safe(self, tool_input: dict) -> bool:
        """MCP 工具默认不并发（保守策略）。"""
        return False


# ============================================================================
# MCPManager —— 管理所有 MCP 连接
# ============================================================================

class MCPManager:
    """
    MCP 连接管理器。

    管理多个 MCP Server 的连接、工具发现和生命周期。
    使用后台事件循环线程维持异步连接，保证 session 在整个生命周期内可用。

    用法：
        manager = MCPManager(cwd="/path/to/project")
        tools = manager.connect_all()  # 同步，返回所有 MCP 工具
        # ... Agent 使用这些工具 ...
        manager.close_all()
    """

    def __init__(self, cwd: str = "."):
        self.cwd = cwd
        self.connections: dict[str, MCPConnection] = {}
        self._configs = load_mcp_config(cwd)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    def _start_loop(self) -> asyncio.AbstractEventLoop:
        """启动后台事件循环线程，返回事件循环。"""
        if self._loop and self._loop.is_running():
            return self._loop

        loop = asyncio.new_event_loop()

        def _run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run_loop, daemon=True)
        thread.start()
        self._loop = loop
        self._loop_thread = thread
        return loop

    def has_servers(self) -> bool:
        """是否配置了 MCP Server。"""
        return len(self._configs) > 0

    def connect_all(self) -> list[Tool]:
        """
        连接所有配置的 MCP Server，返回发现的工具。

        同步方法，内部使用后台事件循环。
        """
        if not self._configs:
            return []

        try:
            loop = self._start_loop()
            future = asyncio.run_coroutine_threadsafe(
                self._connect_all_async(loop), loop
            )
            return future.result(timeout=30)
        except Exception as e:
            print(f"  [MCP] 连接失败: {e}", file=sys.stderr)
            return []

    async def _connect_all_async(self, loop: asyncio.AbstractEventLoop) -> list[Tool]:
        """异步连接所有 MCP Server。"""
        all_tools: list[Tool] = []

        for name, config in self._configs.items():
            conn = MCPConnection(config=config)
            success = await conn.connect(loop=loop)
            if success:
                self.connections[name] = conn
                all_tools.extend(conn.tools)

        return all_tools

    def close_all(self) -> None:
        """关闭所有 MCP 连接并停止后台事件循环。"""
        if not self.connections and not self._loop:
            return
        try:
            if self._loop and self._loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    self._close_all_async(), self._loop
                )
                future.result(timeout=10)
                self._loop.call_soon_threadsafe(self._loop.stop)
                if self._loop_thread:
                    self._loop_thread.join(timeout=5)
        except Exception:
            pass
        self._loop = None
        self._loop_thread = None

    async def _close_all_async(self) -> None:
        """异步关闭所有连接。"""
        for conn in self.connections.values():
            await conn.close()
        self.connections.clear()

    def get_status(self) -> str:
        """获取所有 MCP Server 的连接状态摘要。"""
        if not self._configs:
            return "未配置 MCP Server"

        lines = ["MCP Server 状态:"]
        for name, config in self._configs.items():
            conn = self.connections.get(name)
            if conn and conn.is_connected:
                tool_count = len(conn.tools)
                tool_names = [t._tool_name for t in conn.tools]
                lines.append(f"  {name} [已连接] {tool_count} 个工具: {tool_names}")
            else:
                lines.append(f"  {name} [未连接] ({config.transport}: {config.command})")

        return "\n".join(lines)
