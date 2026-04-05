# 从零实现 Coding Agent（三）：消息历史与多轮对话

前两篇文章实现了 LLM 抽象层和流式输出，但每次调用都是独立的——LLM 不记得你上一句说了什么。这篇文章，我们来解决这个问题。

## LLM 的无状态本质

这是理解 LLM 对话系统的关键：**LLM 本身是无状态的**。

每次调用 API，你发送的是完整的消息列表：

```python
messages = [
    {"role": "user", "content": "我叫小明"},
    {"role": "assistant", "content": "你好，小明！"},
    {"role": "user", "content": "我叫什么名字？"},
]
response = llm.chat(messages)
```

LLM 能回答"你叫小明"，是因为消息列表里包含了之前的对话。如果你只发最后一条：

```python
messages = [{"role": "user", "content": "我叫什么名字？"}]
response = llm.chat(messages)  # LLM 不知道答案
```

所以，多轮对话的本质是：**维护消息历史，每次请求都带上完整历史**。

## 设计 Agent 类

我们需要一个类来管理消息历史：

```python
# agent/agent.py

from dataclasses import dataclass, field
from agent.llm.base import BaseLLM, LLMResponse

@dataclass
class Agent:
    """管理对话状态和消息历史"""

    llm: BaseLLM
    system: str | None = None
    messages: list[dict] = field(default_factory=list)
    turn_count: int = 0

    def chat(self, prompt: str) -> LLMResponse:
        """非流式一轮对话"""
        # 追加用户消息
        self.messages.append({"role": "user", "content": prompt})

        # 调用 LLM
        response = self.llm.chat(self.messages, system=self.system)

        # 追加助手消息
        self.messages.append({"role": "assistant", "content": response.content})
        self.turn_count += 1

        return response

    def stream(self, prompt: str):
        """流式一轮对话"""
        self.messages.append({"role": "user", "content": prompt})

        # 收集完整响应用于历史记录
        full_content = []
        for chunk in self.llm.stream(self.messages, system=self.system):
            full_content.append(chunk)
            yield chunk

        # 流结束后更新历史
        self.messages.append({"role": "assistant", "content": "".join(full_content)})
        self.turn_count += 1

    def clear(self):
        """清空历史"""
        self.messages.clear()
        self.turn_count = 0
```

这里的几个设计点：

1. **`messages` 作为状态**：每次对话自动追加，调用者不用管
2. **`turn_count` 统计**：方便调试和显示
3. **流式模式也要更新历史**：流结束后把完整响应当作 assistant 消息追加

## 交互式 REPL

有了 Agent 类，我们可以实现一个交互式命令行：

```python
# agent/__main__.py

def run_repl(agent: Agent, use_stream: bool):
    """交互式多轮对话循环"""
    print("\n输入问题开始对话，/clear 清空历史，/exit 退出\n")

    while True:
        try:
            prompt = input(f"[{agent.turn_count + 1}] 你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not prompt:
            continue

        if prompt in ("/exit", "/quit"):
            print("再见！")
            break

        if prompt == "/clear":
            agent.clear()
            print("--- 历史已清空，开始新对话 ---\n")
            continue

        # 正常对话
        print("助手: ", end="", flush=True)
        if use_stream:
            for chunk in agent.stream(prompt):
                print(chunk, end="", flush=True)
            print(f"\n  [共 {agent.turn_count} 轮]\n")
        else:
            response = agent.chat(prompt)
            print(response.content)
            print(f"  [输入 {response.input_tokens} / 输出 {response.output_tokens} tokens]\n")
```

运行效果：

```
$ python -m agent

输入问题开始对话，/clear 清空历史，/exit 退出

[1] 你: 我叫小明
助手: 你好小明！有什么我可以帮助你的吗？

[2] 你: 我叫什么名字？
助手: 你说你叫小明。

[3] 你: /clear
--- 历史已清空，开始新对话 ---

[1] 你: 我叫什么名字？
助手: 抱歉，我不知道你的名字。你还没告诉我呢。
```

可以看到，清空历史后 LLM 就"忘记"了之前的对话。

## 消息格式：两种角色

在对话历史中，消息有两种角色：

| role | 含义 | 来源 |
|------|------|------|
| `user` | 用户输入 | 用户的终端输入 |
| `assistant` | 助手回复 | LLM 的输出 |

一个完整的消息历史结构：

```python
[
    {"role": "user", "content": "第一个问题"},
    {"role": "assistant", "content": "第一个回答"},
    {"role": "user", "content": "第二个问题"},
    {"role": "assistant", "content": "第二个回答"},
    # ...
]
```

**注意**：实际发送给 API 时，可能还有 `system` 角色，但那通常不放在 messages 里，而是作为独立参数（Anthropic）或插入 messages 首位（OpenAI）。这在前一篇文章已经处理好了。

## 上下文窗口：隐形的限制

多轮对话有一个隐藏的问题：**上下文窗口有上限**。

不同模型的上下文窗口：

| 模型 | 上下文窗口 |
|------|-----------|
| Claude 3.5 Sonnet | 200K tokens |
| GPT-4o | 128K tokens |
| GPT-3.5-turbo | 16K tokens |

如果对话历史超过窗口大小，API 会报错。这是 coding agent 需要解决的核心问题之一。后续文章我们会实现：

- **Auto Compact**：自动压缩历史，保留关键信息
- **Tool Result Budget**：限制工具输出的长度

现阶段，我们的实现假设对话不会太长。如果超出限制，用户可以用 `/clear` 清空历史。

## 这一步我们学到了什么

1. **LLM 无状态**：每次调用都需要完整历史，"记忆"是客户端维护的
2. **消息历史结构**：user/assistant 角色交替，构成对话上下文
3. **Agent 的职责**：封装消息管理，让调用者无需关心细节
4. **上下文窗口限制**：这是所有 LLM 对话系统都要面对的问题

下一篇文章，我们将实现 **Tool Use 协议**——让 LLM 能够"调用工具"，这是 coding agent 真正开始变得强大的关键一步。
