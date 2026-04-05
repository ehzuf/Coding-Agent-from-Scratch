# 从零实现 Coding Agent（四）：Tool Use 协议

前三篇文章，我们实现了 LLM 调用、流式输出和多轮对话。但这些还只是"聊天机器人"——LLM 只能输出文本，无法真正"做事"。

这篇文章，我们来实现 **Tool Use 协议**——让 LLM 能够调用外部工具。这是 coding agent 从"聊天"升级到"做事"的关键一步。

## Tool Use 的本质

LLM 无法直接执行操作——它不能读文件、不能运行命令、不能访问网络。但 LLM 可以**请求**执行操作。

Tool Use 的流程：

```
用户: "现在几点了？"
    ↓
LLM: "我需要调用 get_current_time 工具"
    ↓
Agent: 执行工具，获取结果 "2026-04-03 15:00:00 UTC"
    ↓
LLM: "现在是 2026 年 4 月 3 日下午 3 点（UTC）"
```

LLM 告诉 Agent 要调用什么工具、传什么参数，Agent 负责执行并把结果回传。这个循环让 LLM 能够间接"操作"外部世界。

## 定义 Tool 接口

首先，我们需要定义工具的统一接口：

```python
# agent/tools/base.py

from abc import ABC, abstractmethod

class Tool(ABC):
    """工具抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，用于 LLM 识别"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，告诉 LLM 这个工具做什么"""
        pass

    @property
    @abstractmethod
    def input_schema(self) -> dict:
        """输入参数的 JSON Schema"""
        pass

    @abstractmethod
    def call(self, input: dict) -> str:
        """执行工具，返回结果字符串"""
        pass

    def to_api_format(self) -> dict:
        """转换为 API 要求的格式"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
```

## 实现第一个工具：get_current_time

让我们实现一个简单的 demo 工具：

```python
# agent/tools/get_current_time.py

from datetime import datetime
from zoneinfo import ZoneInfo
from .base import Tool

class GetCurrentTimeTool(Tool):
    @property
    def name(self) -> str:
        return "get_current_time"

    @property
    def description(self) -> str:
        return "获取指定时区的当前时间"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "时区名称，如 'Asia/Shanghai'、'UTC'、'America/New_York'",
                }
            },
            "required": [],
        }

    def call(self, input: dict) -> str:
        timezone = input.get("timezone", "UTC")
        try:
            tz = ZoneInfo(timezone)
            now = datetime.now(tz)
            return f"当前时间（{timezone}）: {now.strftime('%Y-%m-%d %H:%M:%S')}"
        except Exception as e:
            return f"错误：无效的时区 '{timezone}'"
```

关键点：

- **name**：LLM 会用这个名字来调用工具
- **description**：告诉 LLM 何时应该调用这个工具
- **input_schema**：JSON Schema 格式的参数定义，LLM 据此生成正确的参数

## Tool Use 循环

现在修改 Agent 类，实现 Tool Use 循环：

```python
# agent/agent.py

from dataclasses import dataclass, field

@dataclass
class Agent:
    llm: BaseLLM
    tools: list[Tool] = field(default_factory=list)
    system: str | None = None
    messages: list[dict] = field(default_factory=list)
    turn_count: int = 0
    max_turns: int = 10  # 防止无限循环

    def chat(self, prompt: str) -> LLMResponse:
        self.messages.append({"role": "user", "content": prompt})
        return self._run_tool_loop()

    def _run_tool_loop(self) -> LLMResponse:
        """Tool Use 循环"""
        turn = 0
        while turn < self.max_turns:
            turn += 1

            # 调用 LLM
            api_tools = [t.to_api_format() for t in self.tools]
            response = self.llm.chat(
                self.messages,
                system=self.system,
                tools=api_tools if api_tools else None,
            )

            # 检查是否需要调用工具
            tool_uses = self._extract_tool_uses(response)
            if not tool_uses:
                # LLM 直接回答，追加助手消息并返回
                self.messages.append({"role": "assistant", "content": response.content})
                self.turn_count += 1
                return response

            # 执行所有工具调用
            tool_results = []
            for tool_use in tool_uses:
                tool = self._find_tool(tool_use["name"])
                if tool:
                    result = tool.call(tool_use["input"])
                    tool_results.append({
                        "tool_use_id": tool_use["id"],
                        "result": result,
                    })

            # 将工具结果回传给 LLM
            self._append_tool_result_messages(tool_uses, tool_results)
            # 继续循环，让 LLM 处理工具结果

        raise RuntimeError(f"Tool use exceeded max_turns ({self.max_turns})")
```

## 处理 LLM 响应中的 Tool Use

LLM 的响应不再只是文本，可能包含多个 content block：

```python
def _extract_tool_uses(self, response: LLMResponse) -> list[dict]:
    """从响应中提取工具调用"""
    tool_uses = []
    for block in response.content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_uses.append({
                "id": block["id"],
                "name": block["name"],
                "input": block["input"],
            })
    return tool_uses
```

## 回传工具结果：两种格式

这里有一个关键问题：**Anthropic 和 OpenAI 的工具结果格式不同**。

### Anthropic 格式

工具结果作为 user 消息的 content block：

```python
{
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_123",
            "content": "当前时间: 2026-04-03 15:00:00"
        }
    ]
}
```

### OpenAI 格式

工具结果是独立的 `role: "tool"` 消息：

```python
{
    "role": "tool",
    "tool_call_id": "call_123",
    "content": "当前时间: 2026-04-03 15:00:00"
}
```

我们需要在 Agent 中处理这个差异：

```python
def _append_tool_result_messages(self, tool_uses, tool_results):
    """追加工具结果消息，根据 LLM 类型选择格式"""
    if isinstance(self.llm, AnthropicLLM):
        # Anthropic 格式
        content = []
        for tr in tool_results:
            content.append({
                "type": "tool_result",
                "tool_use_id": tr["tool_use_id"],
                "content": tr["result"],
            })
        self.messages.append({"role": "user", "content": content})
    else:
        # OpenAI 格式
        for tr in tool_results:
            self.messages.append({
                "role": "tool",
                "tool_call_id": tr["tool_use_id"],
                "content": tr["result"],
            })
```

## 运行测试

```bash
python -m agent "现在几点了？北京时间"
```

输出：

```
[anthropic / claude-sonnet-4-20250514] 可用工具: ['get_current_time']

[get_current_time] 现在是北京时间 2026 年 4 月 3 日 15:13:06。
```

LLM 自动判断需要调用工具，Agent 执行后，LLM 用自然语言总结了结果。

## 流式输出 + Tool Use

流式输出和 Tool Use 的结合需要特殊处理。问题是：流式过程中工具调用是逐步"流"出来的，我们无法在流开始时就知道要调用什么工具。

解决方案是引入**事件流**：

```python
@dataclass
class StreamEvent:
    """流式事件"""
    type: str  # "text" | "tool_use_start" | "tool_use_end"
    text: str = ""
    name: str = ""
    result: str = ""
```

修改 `stream()` 方法：

```python
def stream(self, prompt: str) -> Iterator[StreamEvent]:
    self.messages.append({"role": "user", "content": prompt})

    # 收集工具调用
    tool_uses = []
    current_text = []

    for block in self.llm.stream(self.messages, system=self.system, tools=api_tools):
        if block.get("type") == "text_delta":
            current_text.append(block["text"])
            yield StreamEvent(type="text", text=block["text"])

        elif block.get("type") == "tool_use_start":
            yield StreamEvent(type="tool_use_start", name=block["name"])
            tool_uses.append({"id": block["id"], "name": block["name"], "input": {}})

        elif block.get("type") == "tool_input_delta":
            # 累积工具参数
            pass

    # 如果有工具调用，执行并继续流式
    if tool_uses:
        for tool_use in tool_uses:
            tool = self._find_tool(tool_use["name"])
            result = tool.call(tool_use["input"])
            yield StreamEvent(type="tool_use_end", name=tool_use["name"], result=result)

        # 回传结果，继续流式
        # ... 递归调用 stream()
```

CLI 处理事件：

```python
def handle_stream_event(event: StreamEvent):
    if event.type == "text":
        print(event.text, end="", flush=True)
    elif event.type == "tool_use_start":
        print(f"\033[90m[{event.name}]\033[0m ", end="", flush=True)
    elif event.type == "tool_use_end":
        pass  # 静默处理
```

工具调用会以灰色 `[tool_name]` 内联显示，不影响文本的连续性。

## 这一步我们学到了什么

1. **Tool Use 是协作而非执行**：LLM 决定调用什么，Agent 负责执行
2. **工具定义需要清晰**：name、description、input_schema 共同指导 LLM 正确使用
3. **API 格式差异显著**：Anthropic 和 OpenAI 的工具消息格式完全不同，需要抽象处理
4. **循环保护必要**：`max_turns` 防止工具调用陷入无限循环
5. **流式 + Tool Use 需要事件流**：用事件类型区分文本和工具调用

Tool Use 是 coding agent 的核心能力。有了这个基础，后续可以添加更多工具：Bash 执行、文件读写、代码编辑等。

下一篇文章，我们将实现 **Bash 工具**——让 agent 能够执行 shell 命令，这是真正开始"干活"的第一步。
