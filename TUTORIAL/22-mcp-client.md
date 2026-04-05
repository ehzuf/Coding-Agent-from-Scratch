# 从零实现 Coding Agent（二十二）：MCP Client

前几篇我们实现了 Agent 的记忆系统（Session Memory + Auto-Memory）。但 Agent 的能力仍然局限于内置的 7 个工具。如果用户想让 Agent 查询数据库、访问 GitHub API、搜索文档，都需要手动开发新工具。

本篇实现 **MCP Client（Model Context Protocol 客户端）**——让 Agent 连接外部工具服务器，在运行时动态发现和调用工具，大幅扩展能力边界。

## 什么是 MCP

MCP（Model Context Protocol）是 Anthropic 主导的开放协议，定义了 AI Agent 与外部工具服务器之间的标准通信方式：

```
Agent (Client)  ←→  MCP Server  ←→  外部服务
                JSON-RPC 2.0        (数据库、API、文件系统...)
```

核心概念：
- **Server**：暴露工具（tools）、资源（resources）和提示模板（prompts）
- **Client**：连接 Server，发现并调用工具
- **传输层**：stdio（subprocess）、SSE（HTTP）、WebSocket

MCP 的价值在于**标准化**：任何人都可以编写一个 MCP Server，任何 Agent 都可以直接连接使用。生态系统中已有数百个现成的 Server。

## 配置格式

MCP Server 在 `settings.json` 中配置：

```json
{
  "mcpServers": {
    "sqlite": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "./data.db"]
    },
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "ghp_xxx"}
    },
    "docs": {
      "type": "sse",
      "url": "http://localhost:8080/sse"
    }
  }
}
```

支持两级配置：
- 全局：`~/.coding-agent/settings.json`
- 项目：`.coding-agent/settings.json`（同名覆盖全局）

```python
def load_mcp_config(cwd: str = ".") -> dict[str, MCPServerConfig]:
    servers: dict[str, MCPServerConfig] = {}
    # 全局配置
    _load_servers_from_file(
        os.path.join(AGENT_HOME, "settings.json"), servers
    )
    # 项目配置（覆盖同名）
    _load_servers_from_file(
        os.path.join(cwd, ".coding-agent", "settings.json"), servers
    )
    return servers
```

## 连接管理

### MCPConnection

每个 MCP Server 对应一个 `MCPConnection`，管理连接和工具发现：

```python
@dataclass
class MCPConnection:
    config: MCPServerConfig
    tools: list["MCPProxyTool"] = field(default_factory=list)
    _session: object | None = None
    _connected: bool = False
```

连接流程（以 stdio 为例）：

```python
async def connect(self) -> bool:
    from mcp import ClientSession, StdioServerParameters
    from mcp import stdio_client

    # 1. 创建传输层
    server_params = StdioServerParameters(
        command=self.config.command,
        args=self.config.args,
        env=self.config.env,
        cwd=self.config.cwd,
    )

    # 2. 建立连接
    self._stdio_cm = stdio_client(server_params)
    read_stream, write_stream = await self._stdio_cm.__aenter__()

    # 3. 创建会话
    self._session_cm = ClientSession(read_stream, write_stream)
    session = await self._session_cm.__aenter__()
    await session.initialize()

    # 4. 发现工具
    result = await session.list_tools()
    self.tools = [
        MCPProxyTool(
            server_name=self.config.name,
            tool_name=tool.name,
            tool_description=tool.description or "",
            tool_input_schema=tool.inputSchema or {},
            session=session,
        )
        for tool in result.tools
    ]
    return True
```

关键步骤：
1. **创建传输层**：stdio 启动子进程，SSE 连接 HTTP 端点
2. **初始化会话**：JSON-RPC 握手，交换能力声明
3. **发现工具**：调用 `tools/list`，获取工具名称、描述、参数 schema
4. **创建代理**：每个 MCP 工具包装为 `MCPProxyTool`

### MCPManager

`MCPManager` 统一管理所有 Server，使用后台事件循环线程维持异步连接：

```python
class MCPManager:
    def __init__(self, cwd: str = "."):
        self._configs = load_mcp_config(cwd)
        self.connections: dict[str, MCPConnection] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    def _start_loop(self) -> asyncio.AbstractEventLoop:
        """启动后台事件循环线程。"""
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()),
            daemon=True,
        )
        thread.start()
        self._loop = loop
        self._loop_thread = thread
        return loop

    def connect_all(self) -> list[Tool]:
        """连接所有 Server，返回发现的工具。"""
        loop = self._start_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._connect_all_async(loop), loop
        )
        return future.result(timeout=30)

    def close_all(self) -> None:
        """关闭所有连接并停止后台事件循环。"""
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._close_all_async(), self._loop
            )
            future.result(timeout=10)
            self._loop.call_soon_threadsafe(self._loop.stop)
```

关键设计：后台事件循环线程（daemon thread）在 `connect_all()` 时启动，保持运行直到 `close_all()` 被调用。MCP session 和异步上下文管理器绑定在这个循环上，`MCPProxyTool.call()` 通过 `run_coroutine_threadsafe()` 将调用提交到同一个循环，确保 session 始终可用。

## MCPProxyTool

将 MCP 工具代理为 Agent 可用的 `Tool`：

```python
class MCPProxyTool(Tool):
    @property
    def name(self) -> str:
        # 命名空间格式，避免与内置工具冲突
        return f"mcp_{self._server_name}__{self._tool_name}"

    @property
    def description(self) -> str:
        return f"[MCP: {self._server_name}] {self._tool_description}"

    @property
    def input_schema(self) -> dict:
        return self._tool_input_schema

    def call(self, tool_input: dict) -> str:
        # 通过 MCPManager 的持久事件循环执行异步调用
        try:
            if self._loop and self._loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    self._call_async(tool_input), self._loop
                )
                return future.result(timeout=30)
            else:
                return asyncio.run(self._call_async(tool_input))
        except Exception as e:
            return f"MCP 工具调用失败: {e}"
```

### 异步调用

```python
async def _call_async(self, tool_input: dict) -> str:
    result = await self._session.call_tool(self._tool_name, tool_input)

    # 将 MCP 结果转换为纯文本
    parts = []
    for content in result.content:
        if hasattr(content, "text"):
            parts.append(content.text)
        elif hasattr(content, "data"):
            parts.append(f"[Binary data: {content.mimeType}]")
        else:
            parts.append(str(content))

    return "\n".join(parts) if parts else "(empty result)"
```

MCP 工具的结果是 content blocks 列表，可能包含文本、图片、二进制数据等。我们将文本内容拼接，非文本内容标记类型。

## Agent 集成

在 `build_agent()` 中，MCP 工具在内置工具之后加载：

```python
# 加载内置工具
tools = get_tools()

# MCP Client：连接 MCP Server，发现远程工具
mcp_manager = MCPManager(cwd=config.cwd)
if mcp_manager.has_servers():
    mcp_tools = mcp_manager.connect_all()
    if mcp_tools:
        tools.extend(mcp_tools)
```

对 Agent 来说，MCP 工具和内置工具没有区别——都是 `Tool` 子类，都通过 `call()` 执行。LLM 根据工具描述自动选择合适的工具。

启动时会显示 MCP 连接信息：

```
[anthropic / claude-sonnet-4-20250514] 工作目录: /Users/xxx/project
  可用工具: [get_current_time, bash, read, ..., mcp_sqlite__query, mcp_sqlite__execute]
  MCP: 1 个 Server 已连接，2 个工具 (['sqlite'])
```

## REPL 命令

`/mcp` 查看 MCP 连接状态：

```
[1] 你: /mcp

MCP Server 状态:
  sqlite [已连接] 2 个工具: ['query', 'execute']
  github [未连接] (stdio: npx)
```

## 使用示例

### 连接 SQLite MCP Server

```json
{
  "mcpServers": {
    "sqlite": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "./data.db"]
    }
  }
}
```

```
你: 查询 users 表中的所有数据
助手: [mcp_sqlite__query] 
users 表中有 3 条记录：
| id | name | email |
|---|---|---|
| 1 | Alice | alice@example.com |
| 2 | Bob | bob@example.com |
```

### 连接 GitHub MCP Server

```json
{
  "mcpServers": {
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "ghp_xxx"}
    }
  }
}
```

```
你: 列出仓库最近的 issues
助手: [mcp_github__list_issues]
最近的 issues：
1. #12 - 实现 MCP 支持
2. #11 - 添加 Auto-Memory
```

## 与 Claude Code 的差异

| 方面 | Claude Code | 我们的实现 |
|---|---|---|
| 传输层 | stdio + SSE + HTTP + WebSocket | stdio + SSE |
| 资源访问 | ListMcpResourcesTool + ReadMcpResourceTool | 仅工具代理（MVP） |
| 健康检查 | 心跳 + 自动重连 | 连接失败报错（MVP） |
| 生命周期 | 注册清理回调 | 进程退出时关闭 |
| 工具命名 | `mcp__server__tool` | `mcp_server__tool` |

## 小结

MCP Client 让 Agent 的能力从"内置 7 个工具"扩展到"生态系统中任意工具"：

- **标准协议**：遵循 MCP 规范，兼容所有 MCP Server
- **动态发现**：运行时连接 Server 并发现工具
- **透明代理**：MCP 工具和内置工具统一接口，LLM 无差别使用
- **两级配置**：全局 + 项目级，灵活管理 Server

下一篇我们将实现 **Skills 系统**——用 markdown 文件定义自定义技能，让用户可以创建可复用的工作流模板。
