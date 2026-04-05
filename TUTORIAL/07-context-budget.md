# 从零实现 Coding Agent（七）：上下文预算管理

随着对话轮数增加，消息历史会不断增长。如果不加控制，很快就会超出 LLM 的上下文窗口限制。这两篇文章介绍两种应对策略：Tool Result Budget 和 Auto Compact。

## 问题的本质

LLM 有上下文窗口限制：

| 模型 | 上下文窗口 |
|------|-----------|
| Claude 3.5 Sonnet | 200K tokens |
| GPT-4o | 128K tokens |
| GPT-3.5-turbo | 16K tokens |

超出限制会导致：
1. API 报错，请求失败
2. 成本激增（token 越多费用越高）
3. 响应质量下降（模型难以关注长上下文的所有细节）

问题的来源有两个：
1. **Tool Result 过大**：读取大文件、执行输出冗长的命令
2. **消息历史过长**：多轮对话累积

## Tool Result Budget

### 策略：截断

对于单个 tool result，如果超过阈值，直接截断。策略是保留开头和结尾，中间省略。

```python
# agent/budget.py

def truncate_tool_result(
    content: str,
    max_length: int = 10000,
    head_length: int = 3000,
    tail_length: int = 3000,
) -> str:
    """截断 tool result，防止超过上下文窗口。"""
    if len(content) <= max_length:
        return content

    head = content[:head_length]
    tail = content[-tail_length:]
    omitted = len(content) - head_length - tail_length

    return (
        f"{head}\n"
        f"... [内容已截断，省略 {omitted} 字符，共 {len(content)} 字符] ...\n"
        f"{tail}"
    )
```

### 为什么保留开头和结尾？

- **开头**：通常包含文件头、命令概述、关键信息
- **结尾**：通常包含最终结果、错误信息、总结
- **中间**：往往是重复或细节内容

### 集成到 Agent

```python
# agent/agent.py

from agent.budget import truncate_tool_result

class Agent:
    def __init__(
        self,
        ...,
        enable_budget: bool = True,
        max_tool_result_length: int = 10000,
    ):
        self.enable_budget = enable_budget
        self.max_tool_result_length = max_tool_result_length

    def _execute_tool(self, tool_use: dict) -> str:
        result = tool.call(tool_input)

        # 应用 budget 控制
        if self.enable_budget:
            result = truncate_tool_result(result, self.max_tool_result_length)

        return result
```

### 使用示例

```python
from agent.agent import Agent
from agent.llm import create_llm
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
agent = Agent(
    llm=llm,
    tools=get_tools(),
    enable_budget=True,
    max_tool_result_length=5000,
)

# 读取大文件时会自动截断
response = agent.chat("读取 /var/log/syslog")
```

输出：
```
Mar 10 08:30:01 server cron[1234]: (root) CMD (/usr/bin/backup)
Mar 10 08:31:15 server sshd[5678]: Accepted publickey for user from 192.168.1.100
...
[数百行日志]
...

... [内容已截断，省略 45000 字符，共 50000 字符] ...

Mar 15 18:45:22 server kernel: [1234567.890123] Out of memory: Kill process 9999
Mar 15 18:45:23 server systemd[1]: session-123.scope: Succeeded.
```

## Auto Compact

### 策略：压缩

对于消息历史，当接近阈值时，用 LLM 生成摘要，替换详细内容。

```python
# agent/compact.py

def compact_messages(
    messages: list[dict],
    llm: BaseLLM,
    keep_recent: int = 4,
) -> CompactResult:
    """
    压缩消息历史。

    策略：
      1. 保留最近 keep_recent 轮完整对话
      2. 对更早的历史生成摘要
      3. 用摘要替换原始消息
    """
    # 分离 system prompt
    system_msg = None
    other_messages = []
    for msg in messages:
        if msg.get("role") == "system" and system_msg is None:
            system_msg = msg
        else:
            other_messages.append(msg)

    # 保留最近的消息
    keep_count = min(keep_recent * 2, len(other_messages))
    recent_messages = other_messages[-keep_count:]
    old_messages = other_messages[:-keep_count]

    # 生成摘要
    history_text = format_messages(old_messages)
    summary = llm.chat(
        messages=[{"role": "user", "content": f"请摘要以下对话：\n{history_text}"}],
        system="你是一个对话摘要助手。",
    ).text

    # 构建新的消息列表
    # 注意：摘要作为 role="user" 消息存入，与 Claude Code 一致
    # （Claude Code 的 summaryMessages 类型是 UserMessage[]）
    new_messages = []
    if system_msg:
        new_messages.append(system_msg)
    new_messages.append({
        "role": "user",
        "content": f"[历史摘要] {summary}",
    })
    new_messages.extend(recent_messages)

    return CompactResult(...)
```

### 为什么保留最近几轮？

- **最近对话通常最重要**：包含当前任务的上下文
- **旧对话可以抽象**：只需要知道"讨论了什么"，不需要知道"具体说了什么"
- **平衡质量和成本**：保留太多失去压缩意义，保留太少丢失上下文

> **教学简化**：我们的摘要 prompt 是简单的一句话。Claude Code 使用 9 段式结构化 prompt（包含 `<analysis>` 分析步骤和 `<summary>` 输出格式），并在摘要末尾附加"Continue the conversation from where it left off without asking the user any further questions..."等续接指令。生产环境建议参考其设计以提升摘要质量。

### 集成到 Agent

```python
# agent/agent.py

from agent.compact import maybe_compact
from agent.budget import check_context_budget

class Agent:
    def __init__(
        self,
        ...,
        enable_compact: bool = True,
        compact_threshold: int = 80000,
    ):
        self.enable_compact = enable_compact
        self.compact_threshold = compact_threshold

    def _check_and_compact(self) -> None:
        """检查上下文预算，如果需要则执行压缩。"""
        if not self.enable_compact:
            return

        budget_info = check_context_budget(self.messages, self.compact_threshold)
        if budget_info["is_warning"]:
            new_messages, result = maybe_compact(
                self.messages, self.llm, self.compact_threshold
            )
            if result:
                self.messages = new_messages
```

### 使用示例

```python
from agent.agent import Agent
from agent.llm import create_llm
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
agent = Agent(
    llm=llm,
    tools=get_tools(),
    enable_compact=True,
    compact_threshold=60000,
)

# 进行多轮对话，历史会自动压缩
for i in range(50):
    response = agent.chat(f"第 {i} 轮对话，讨论功能实现...")
```

## 截断 vs 压缩：如何选择？

| | Tool Result Budget | Auto Compact |
|---|---|---|
| **目标** | 单个 tool result | 整个消息历史 |
| **策略** | 截断（丢失信息） | 摘要（保留语义） |
| **速度** | 快（本地处理） | 慢（需要 LLM 调用） |
| **成本** | 无额外成本 | 需要额外 LLM 调用 |
| **适用场景** | 大文件、长日志 | 长对话历史 |

两者可以并用：
- Tool Result Budget 防止单个输出过大
- Auto Compact 防止历史累积过长

## 估算 Token 数

准确的 token 数需要模型的 tokenizer，但我们可以粗略估计：

```python
def count_tokens_approx(text: str) -> int:
    """估算 token 数（粗略估计）。"""
    # 保守估计：每 3 个字符约 1 个 token
    return len(text) // 3 + 1
```

实际比例：
- 英文：约 4 字符/token
- 中文：约 1-2 字符/token
- 代码：约 3-4 字符/token

我们使用保守的 3 字符/token，确保不会低估。

## 这一步我们学到了什么

1. **上下文是有限的资源**：必须主动管理，否则会溢出
2. **截断是快速但损失信息的方案**：适合 tool result 这种"一次性"内容
3. **压缩是保留语义但消耗资源的方案**：适合消息历史这种需要"记忆"的内容
4. **阈值需要权衡**：太激进影响质量，太保守浪费资源

下一篇文章，我们将实现 **Max Turns 保护 + 错误处理**——让 agent 更健壮地应对各种异常情况。
