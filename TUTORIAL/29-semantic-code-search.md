# 从零实现 Coding Agent（二十九）：语义级代码搜索

前面我们实现了 GrepTool（正则文本搜索）和 GlobTool（文件路径匹配）。这两个工具让 Agent 能在项目中找到文件和文本，但它们有一个根本局限：**把代码当纯文本处理**。

一个典型场景：用户让 Agent "重构 Tool 基类的 call 方法"。Agent 用 grep 搜 `call`——返回几十个匹配，函数定义、方法调用、注释里的提及、字符串里的文本混在一起。Agent 无法区分"call 在哪里定义"和"call 在哪里被调用"。

本篇解决这个问题：让 Agent 理解代码的**语法结构**，精确找到定义、引用、类层次。两种方案——先用 tree-sitter 做轻量级 AST 解析，再用 LSP 做完整语义分析。

---

## Grep 的局限

用我们项目自己的代码举例。Agent 想找 `call` 方法的定义：

```
> grep pattern="def call" path="agent/tools" include="*.py"

找到 17 处匹配:

agent/tools/base.py:71:     def call(self, input: dict[str, Any]) -> str:
agent/tools/bash.py:145:     def call(self, input: dict[str, Any]) -> str:
agent/tools/edit.py:73:     def call(self, input: dict[str, Any]) -> str:
agent/tools/glob.py:67:     def call(self, input: dict[str, Any]) -> str:
agent/tools/grep.py:71:     def call(self, input: dict[str, Any]) -> str:
agent/tools/read.py:65:     def call(self, input: dict[str, Any]) -> str:
agent/tools/write.py:59:     def call(self, input: dict[str, Any]) -> str:
agent/tools/symbol_search.py:180:     def call(self, input: dict[str, Any]) -> str:
...（还有 background/tools.py, plan_mode.py 等 9 条）
```

用 `def call` 能勉强过滤出定义。但如果任务更复杂呢？

| 查询 | Grep 能做吗 | 为什么 |
|------|------------|--------|
| 找 `call` 方法的定义 | 近似（`def call`） | 无法区分方法和普通函数 |
| 找所有继承 `Tool` 的子类 | 做不到 | 无法解析继承关系 |
| 找所有调用 `tool.call()` 的位置 | 做不到 | 无法区分定义和调用 |
| 列出一个文件里的所有类和方法 | 做不到 | 正则难以处理嵌套结构 |

根本原因：grep 匹配的是**字符模式**，不理解代码的**语法结构**。要解决这些问题，需要把源代码解析成语法树。

---

## 方案一：tree-sitter（轻量 AST 解析）

[tree-sitter](https://tree-sitter.github.io/tree-sitter/) 是一个增量解析器生成框架。它能在毫秒级别把源代码解析成具体语法树（Concrete Syntax Tree），支持 40+ 种语言。

关键特点：
- **纯库调用**——不需要启动外部进程，import 即用
- **速度快**——单个文件解析在毫秒级
- **增量解析**——文件修改后只重新解析变更部分（适合编辑器场景）

Python 绑定：`tree-sitter`（核心库）+ `tree-sitter-python`（Python 语法包）。

### 解析源代码

```python
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY_LANGUAGE = Language(tspython.language())

parser = Parser(PY_LANGUAGE)

code = b'''
class Foo:
    def bar(self, x: int) -> str:
        return str(x)

def standalone():
    pass
'''

tree = parser.parse(code)
root = tree.root_node
```

解析后得到一棵语法树。每个节点有 `type`（语法类型）和 `children`（子节点）。Python 代码中我们关心的节点类型：

| 节点类型 | 含义 | 例子 |
|---------|------|------|
| `class_definition` | 类定义 | `class Foo:` |
| `function_definition` | 函数/方法定义 | `def bar(self):` |
| `identifier` | 标识符（名称） | `Foo`, `bar`, `self` |
| `call` | 函数调用 | `str(x)` |

遍历语法树，就能区分"定义"和"调用"——这正是 grep 做不到的事。

通过 `child_by_field_name` 可以提取节点的特定部分：

```python
# 假设 node 是一个 class_definition
name_node = node.child_by_field_name("name")    # → identifier "Foo"
body_node = node.child_by_field_name("body")     # → block（类体）

# 假设 node 是一个 function_definition
name_node = node.child_by_field_name("name")     # → identifier "bar"
```

### 符号提取

有了语法树遍历能力，就可以从文件中提取所有符号定义。先定义一个 `Symbol` 数据结构：

```python
# agent/tools/symbol_search.py

import os
from dataclasses import dataclass
from typing import Any

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from .base import Tool

PY_LANGUAGE = Language(tspython.language())


@dataclass
class Symbol:
    """代码符号"""
    name: str           # 符号名，如 "GrepTool", "call"
    kind: str           # "class", "function", "method"
    file_path: str
    line: int           # 起始行号
    end_line: int       # 结束行号
    parent: str | None  # 所属类名（方法时非 None）
```

然后实现提取函数——遍历语法树，遇到 `class_definition` 或 `function_definition` 就记录：

```python
def extract_symbols(file_path: str) -> list[Symbol]:
    """从 Python 文件中提取所有符号定义"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source.encode())
    symbols: list[Symbol] = []

    def visit(node, parent_class: str | None = None):
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                class_name = name_node.text.decode()
                symbols.append(Symbol(
                    name=class_name,
                    kind="class",
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    parent=parent_class,
                ))
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        visit(child, class_name)
            return

        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                symbols.append(Symbol(
                    name=name_node.text.decode(),
                    kind="method" if parent_class else "function",
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    parent=parent_class,
                ))
            return

        for child in node.children:
            visit(child, parent_class)

    visit(tree.root_node)
    return symbols
```

关键设计：
- `parent_class` 参数通过递归传递，用于区分"函数"和"方法"
- 遇到 `class_definition` 后进入类体时传入类名，这样内部的 `function_definition` 就被标记为 `method`
- `start_point[0]` 是从 0 开始的行号，+1 转为人类习惯的从 1 开始

对项目自身运行：

```
> extract_symbols("agent/tools/base.py")

Symbol(name='Tool', kind='class', file_path='agent/tools/base.py', line=27, end_line=114, parent=None)
Symbol(name='name', kind='method', ..., line=40, end_line=42, parent='Tool')
Symbol(name='description', kind='method', ..., line=46, end_line=48, parent='Tool')
Symbol(name='input_schema', kind='method', ..., line=52, end_line=68, parent='Tool')
Symbol(name='call', kind='method', ..., line=71, end_line=82, parent='Tool')
Symbol(name='is_concurrency_safe', kind='method', ..., line=84, end_line=101, parent='Tool')
Symbol(name='to_api_format', kind='method', ..., line=103, end_line=114, parent='Tool')
```

现在 Agent 能看到文件的完整结构了——哪些类、哪些方法、每个方法从哪行到哪行。

### SymbolSearchTool

把符号提取封装成 Agent 工具，继承 `Tool` 基类：

```python
class SymbolSearchTool(Tool):
    """语义级代码搜索工具"""

    @property
    def name(self) -> str:
        return "symbol_search"

    @property
    def description(self) -> str:
        return """语义级代码搜索。基于语法树解析，能区分定义和引用。

三种操作：
- definitions: 查找符号定义（函数、类、方法的声明位置）
- symbols: 列出文件或目录中的所有符号
- references: 查找符号被引用（非定义）的位置

比 grep 更精确：grep 搜 "call" 会匹配注释和字符串，
symbol_search 只匹配代码结构中的标识符。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["definitions", "symbols", "references"],
                    "description": "操作类型",
                },
                "name": {
                    "type": "string",
                    "description": "要搜索的符号名（definitions/references 时必需）",
                },
                "path": {
                    "type": "string",
                    "description": "搜索目录或文件（默认当前目录）",
                },
                "include": {
                    "type": "string",
                    "description": "文件过滤，如 *.py",
                },
            },
            "required": ["action"],
        }

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def call(self, input: dict[str, Any]) -> str:
        action = input.get("action", "")
        name = input.get("name", "")
        path = input.get("path", ".")
        include = input.get("include")

        if action == "definitions":
            return self._find_definitions(name, path, include)
        elif action == "symbols":
            return self._list_symbols(path, include)
        elif action == "references":
            return self._find_references(name, path, include)
        else:
            return f"未知操作: {action}。支持: definitions, symbols, references"
```

三个 action 的输出格式设计：

**definitions** —— 查找符号定义：

```
> symbol_search action="definitions" name="call" path="agent/tools"

找到 17 处定义:

  method Tool.call (agent/tools/base.py:71-82)
  method BashTool.call (agent/tools/bash.py:145-187)
  method EditTool.call (agent/tools/edit.py:73-124)
  method GlobTool.call (agent/tools/glob.py:67-103)
  method GrepTool.call (agent/tools/grep.py:71-138)
  method ReadTool.call (agent/tools/read.py:65-128)
  method WriteTool.call (agent/tools/write.py:59-83)
  method SymbolSearchTool.call (agent/tools/symbol_search.py:180-193)
  ...（还有 AgentTool, SleepTool 等 9 处）
```

同样是 17 条匹配，但 symbol_search 告诉你每个 `call` 属于哪个类、是方法不是函数、精确的行号范围。grep 给不了这些信息。

**symbols** —— 列出文件的符号结构：

```
> symbol_search action="symbols" path="agent/tools/base.py"

agent/tools/base.py:
  class Tool (27-114)
    method name (40-42)
    method description (46-48)
    method input_schema (52-68)
    method call (71-82)
    method is_concurrency_safe (84-101)
    method to_api_format (103-114)
```

**references** —— 查找引用（非定义位置的标识符匹配）：

```
> symbol_search action="references" name="Tool" path="agent/tools/__init__.py"

找到 6 处引用:

  agent/tools/__init__.py:10: from .base import Tool
  agent/tools/__init__.py:29: BUILTIN_TOOLS: list[Tool] = [
  agent/tools/__init__.py:45: def get_tools() -> list[Tool]:
  agent/tools/__init__.py:50: def find_tool(name: str, tools: list[Tool] | None = None) -> Tool | None:
  agent/tools/__init__.py:50: def find_tool(name: str, tools: list[Tool] | None = None) -> Tool | None:
  agent/tools/__init__.py:59: def to_api_tools(tools: list[Tool] | None = None) -> list[dict]:
```

引用查找的实现原理：遍历语法树中所有 `identifier` 节点，找到名称匹配的，排除掉在定义位置（`class_definition` 或 `function_definition` 的 `name` 字段）的那些。

### tree-sitter 的局限

tree-sitter 在**单文件**级别工作：

- **不解析 import**——如果 A 文件 `from b import Foo`，在 A 文件里搜 `Foo` 的定义不会跳转到 B 文件
- **引用匹配基于名称**——两个不同类各有一个 `call` 方法，tree-sitter 无法区分 `a.call()` 和 `b.call()` 引用的是哪个
- **不做类型推断**——无法判断 `x.process()` 中 `x` 的类型，也就无法找到 `process` 的定义

这些都需要跨文件的语义分析。这就是 LSP 的领域。

---

## 方案二：LSP（语言服务器协议）

LSP（Language Server Protocol）是微软发起的标准协议，用于编辑器和语言服务器之间的通信。VS Code、Neovim、Emacs 都通过 LSP 实现代码智能功能。

语言服务器维护整个项目的**语义模型**——它知道每个变量的类型、每个 import 指向哪个文件、每个方法属于哪个类。这比 tree-sitter 的单文件语法解析强大得多，但代价是需要启动和管理一个服务器进程。

### LSP 协议基础

LSP 使用 JSON-RPC 2.0 over stdio 通信。基本交互流程：

```
Client（Agent）                    Server（语言服务器）
  │                                    │
  │──── initialize ───────────────────▶│  能力协商
  │◀─── capabilities ─────────────────│
  │──── initialized ──────────────────▶│
  │                                    │
  │──── textDocument/didOpen ─────────▶│  打开文件
  │──── textDocument/definition ──────▶│  跳转到定义
  │◀─── Location[] ───────────────────│
  │──── textDocument/references ──────▶│  查找所有引用
  │◀─── Location[] ───────────────────│
  │                                    │
  │──── shutdown ─────────────────────▶│  关闭
  │──── exit ─────────────────────────▶│
```

一次 `textDocument/definition` 的请求和响应：

```json
// 请求：第 71 行第 8 列的符号定义在哪？
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "textDocument/definition",
  "params": {
    "textDocument": {"uri": "file:///project/agent/tools/__init__.py"},
    "position": {"line": 27, "character": 15}
  }
}

// 响应：定义在 base.py 第 27 行
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": [{
    "uri": "file:///project/agent/tools/base.py",
    "range": {"start": {"line": 26, "character": 0}, "end": {"line": 113, "character": 0}}
  }]
}
```

注意 LSP 的行号和列号都从 0 开始。

### LSPClient 实现

一个最小可用的 LSP 客户端需要处理三件事：进程管理、JSON-RPC 通信、LSP 握手。

> 教学简化：生产级 LSP 客户端需要处理异步通知（diagnostics）、进度报告、文件监视等。这里只实现同步请求-响应模式，聚焦核心原理。

```python
# agent/tools/lsp_client.py

import json
import subprocess
from pathlib import Path


class LSPClient:
    """轻量 LSP 客户端"""

    def __init__(self, command: list[str], root_path: str):
        """
        command: 启动语言服务器的命令，如 ["pylsp"]
        root_path: 项目根目录
        """
        self.command = command
        self.root_path = str(Path(root_path).resolve())
        self._process: subprocess.Popen | None = None
        self._request_id = 0

    def start(self) -> None:
        """启动语言服务器并完成 initialize 握手"""
        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # initialize 握手
        self._request("initialize", {
            "processId": None,
            "rootUri": f"file://{self.root_path}",
            "capabilities": {},
        })
        # 通知服务器初始化完成
        self._notify("initialized", {})

    def _ensure_open(self, file_path: str) -> str:
        """确保文件已通知服务器打开，返回 URI"""
        uri = f"file://{Path(file_path).resolve()}"
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "python",
                "version": 1,
                "text": Path(file_path).read_text(),
            },
        })
        return uri

    def get_definition(self, file_path: str, line: int, character: int) -> list[dict]:
        """查找定义位置（行号从 0 开始）"""
        uri = self._ensure_open(file_path)
        result = self._request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })
        if isinstance(result, dict):
            return [result]
        return result or []

    def get_references(self, file_path: str, line: int, character: int) -> list[dict]:
        """查找所有引用位置"""
        uri = self._ensure_open(file_path)
        result = self._request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": True},
        })
        return result or []

    def get_document_symbols(self, file_path: str) -> list[dict]:
        """获取文件中的所有符号"""
        uri = self._ensure_open(file_path)
        result = self._request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })
        return result or []

    def shutdown(self) -> None:
        """关闭语言服务器"""
        if self._process:
            self._request("shutdown", None)
            self._notify("exit", None)
            self._process.terminate()
            self._process = None

    def _request(self, method: str, params) -> dict | list | None:
        """发送 JSON-RPC 请求并等待响应"""
        self._request_id += 1
        msg = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        return self._receive()

    def _notify(self, method: str, params) -> None:
        """发送 JSON-RPC 通知（无 id，不等响应）"""
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _send(self, msg: dict) -> None:
        """写入 LSP 消息：Content-Length header + JSON body"""
        body = json.dumps(msg).encode()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        self._process.stdin.write(header + body)
        self._process.stdin.flush()

    def _receive(self) -> dict | list | None:
        """读取一条 LSP 响应"""
        # 读取 Content-Length header
        headers = {}
        while True:
            line = self._process.stdout.readline().decode().strip()
            if not line:
                break
            key, value = line.split(": ", 1)
            headers[key] = value

        length = int(headers["Content-Length"])
        body = self._process.stdout.read(length).decode()
        response = json.loads(body)

        # 跳过通知（没有 id 的消息），继续读取
        if "id" not in response:
            return self._receive()

        return response.get("result")
```

核心要点：
- **wire format**：每条消息前加 `Content-Length: N\r\n\r\n` header，后接 JSON body
- **request vs notification**：request 有 `id` 字段，需要等响应；notification 没有 `id`，发完即走
- **跳过通知**：语言服务器可能主动推送通知（如诊断信息），`_receive` 遇到没有 `id` 的消息时递归读取下一条

### LSPTool

把 LSP 客户端封装为 Agent 工具：

```python
class LSPTool(Tool):
    """基于语言服务器的精确代码导航工具"""

    def __init__(self, lsp_client: LSPClient):
        self._client = lsp_client

    @property
    def name(self) -> str:
        return "lsp"

    @property
    def description(self) -> str:
        return """精确代码导航（基于语言服务器）。

三种操作：
- definition: 跳转到定义（支持跨文件、解析 import）
- references: 查找所有引用（类型感知，比 grep 精确）
- document_symbols: 列出文件中的所有符号"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["definition", "references", "document_symbols"],
                },
                "file": {"type": "string", "description": "文件路径"},
                "line": {"type": "integer", "description": "行号（从 1 开始）"},
                "character": {"type": "integer", "description": "列号（从 1 开始）"},
            },
            "required": ["action", "file"],
        }

    def call(self, input: dict[str, Any]) -> str:
        action = input["action"]
        file_path = input["file"]
        line = input.get("line", 1) - 1         # 转为 LSP 的 0-based
        character = input.get("character", 1) - 1

        if action == "definition":
            locations = self._client.get_definition(file_path, line, character)
            return self._format_locations("定义", locations)
        elif action == "references":
            locations = self._client.get_references(file_path, line, character)
            return self._format_locations("引用", locations)
        elif action == "document_symbols":
            symbols = self._client.get_document_symbols(file_path)
            return self._format_symbols(symbols)
        return f"未知操作: {action}"

    def _format_locations(self, label: str, locations: list[dict]) -> str:
        if not locations:
            return f"未找到{label}"
        lines = [f"找到 {len(locations)} 处{label}:", ""]
        for loc in locations:
            uri = loc.get("uri", "")
            path = uri.replace("file://", "")
            start = loc.get("range", {}).get("start", {})
            lines.append(f"  {path}:{start.get('line', 0) + 1}:{start.get('character', 0) + 1}")
        return "\n".join(lines)

    def _format_symbols(self, symbols: list[dict]) -> str:
        if not symbols:
            return "未找到符号"
        lines = []
        for sym in symbols:
            kind_name = self._symbol_kind(sym.get("kind", 0))
            start_line = sym.get("range", {}).get("start", {}).get("line", 0) + 1
            lines.append(f"  {kind_name} {sym.get('name', '?')} (line {start_line})")
            for child in sym.get("children", []):
                child_kind = self._symbol_kind(child.get("kind", 0))
                child_line = child.get("range", {}).get("start", {}).get("line", 0) + 1
                lines.append(f"    {child_kind} {child.get('name', '?')} (line {child_line})")
        return "\n".join(lines)

    @staticmethod
    def _symbol_kind(kind: int) -> str:
        kinds = {1: "file", 2: "module", 3: "namespace", 5: "class",
                 6: "method", 9: "constructor", 12: "function", 13: "variable"}
        return kinds.get(kind, f"kind({kind})")
```

关键设计：
- **行号转换**：Agent 和人类习惯从 1 开始，LSP 从 0 开始。`LSPTool` 在接口层做转换，对内对外各自一致
- **不加入 BUILTIN_TOOLS**：LSP 需要启动语言服务器进程（如 `pylsp`），是可选的重量级能力，应按项目需要配置，而不是默认加载

使用方式：

```python
# 配置 LSP（项目启动时）
lsp = LSPClient(command=["pylsp"], root_path="/path/to/project")
lsp.start()

# 注册为工具
agent.tools.append(LSPTool(lsp))

# Agent 自动在需要时调用
# "找到 file_manager.process() 的定义" → lsp(action="definition", file="...", line=42, character=15)
```

---

## 对比：tree-sitter vs LSP vs Grep

| 维度 | Grep | tree-sitter | LSP |
|------|------|-------------|-----|
| 速度 | 极快 | 快（毫秒/文件） | 慢（服务器启动 + 索引） |
| 精度 | 文本级 | 语法级 | 语义级 |
| 跨文件 | 文本匹配 | 不支持 | 支持（解析 import） |
| 安装成本 | 无 | pip install | 需要语言服务器进程 |
| 语言支持 | 任意文本 | 按语法包 | 按语言服务器 |
| 找定义 | 近似 | 语法准确 | 完全准确 |
| 找引用 | 文本匹配 | 名称匹配 | 类型感知 |
| 类型信息 | 无 | 无 | 有 |
| 运行时依赖 | 无 | tree-sitter 库 | 外部进程 |

**何时用哪个：**

- **Grep**：快速文本搜索、日志搜索、配置文件搜索、非代码文本
- **tree-sitter**：理解代码结构但不需要跨文件精度——列出符号、找定义、按语法类型过滤
- **LSP**：需要跨文件精确导航、重构辅助、类型感知的场景

实践中三者互补。Agent 可以先用 grep 快速定位大致范围，再用 symbol_search 确认定义位置，必要时用 lsp 做跨文件跳转。

---

## 集成到 Agent

`SymbolSearchTool`（tree-sitter）作为内建工具注册，和 GrepTool 一起提供：

```python
# agent/tools/__init__.py

from .symbol_search import SymbolSearchTool

BUILTIN_TOOLS: list[Tool] = [
    ...
    GrepTool(),
    SymbolSearchTool(),    # 新增
    ...
]
```

`LSPTool` 不放入 `BUILTIN_TOOLS`——它依赖外部语言服务器进程，应该像 MCP 一样按项目配置。

有了多种搜索工具，Agent 可以根据任务选择最合适的方式。例如重构一个方法：

```
用户: 重构 Tool.call 的签名，加一个 context 参数

Agent 的工具调用序列:
  1. symbol_search(action="definitions", name="call", path="agent/tools")
     → 找到基类 Tool.call 和所有子类的 call 定义
  2. symbol_search(action="references", name="call", path="agent/")
     → 找到所有调用 call() 的位置
  3. read(...) → 逐一读取相关文件
  4. edit(...) → 逐一修改签名和调用
```

比起盲目 grep 再人工过滤，这个流程更精确也更高效。

---

## 小结

语义级代码搜索解决的核心问题：**让 Agent 像程序员一样理解代码结构，而不是像搜索引擎一样匹配字符串。**

关键设计决策：

1. **文本搜索 ≠ 代码理解** — grep 匹配字符模式，无法区分定义、调用、注释中的同名标识符
2. **tree-sitter 是轻量级利器** — 纯库调用，毫秒级解析，提取函数/类/方法定义，无需外部进程
3. **LSP 提供完整语义** — 通过语言服务器实现跨文件定义跳转、类型感知的引用查找，但代价是进程管理
4. **工具分层互补** — Grep（文本）→ tree-sitter（语法）→ LSP（语义），三层搜索覆盖不同精度需求
5. **Agent 自主选择** — 把多种搜索工具都暴露给 LLM，由它根据任务特征选择最合适的工具
