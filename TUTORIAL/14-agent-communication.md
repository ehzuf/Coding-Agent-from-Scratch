# 从零实现 Coding Agent（十四）：Agent 间通信

在上一篇中，我们实现了子 Agent：主 Agent 可以启动独立的子 Agent 来执行子任务。但那时的子 Agent 是"一次性"的——执行完任务返回结果，然后就被丢弃了。

这带来几个问题：

1. **无法追问**：子 Agent 返回了一份分析报告，主 Agent 想要更多细节，只能启动一个全新的子 Agent 重新建立上下文
2. **信息不透明**：主 Agent 只能看到子 Agent 的文本结果，不知道它用了多少 token、调用了几次工具、花了多长时间
3. **角色固定**：所有子 Agent 都用同一个 system prompt，无法为特定任务定制角色

本篇解决这三个问题：**结构化输出**、**Agent 注册表 + SendMessage**、**自定义 system prompt**。

---

## 问题一：子 Agent 的返回不够"结构化"

之前的子 Agent 返回是这样的：

```
[子 Agent: 搜索工具文件]
tools/ 目录下有 9 个 Python 文件...

--- 子 Agent 统计: turns=3, messages=8, input_tokens=1200, output_tokens=450 ---
```

这有几个问题：
- 没有唯一标识——如果同时启动了 3 个子 Agent，分不清谁是谁
- 统计信息不完整——只有最后一次 LLM 调用的 token 数，不是整个执行过程的累计
- 没有状态字段——无法程序化地判断成功/失败

### 解决：Agent 统计追踪 + 结构化输出

首先，给 Agent 类加上累计统计：

```python
class Agent:
    def __init__(self, ...):
        # ...
        # 统计信息
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._tool_use_count: int = 0
```

在 `_run_tool_loop()` 中，每次收到 LLM 响应后累加：

```python
while turn_count < self.max_turns:
    response = self._call_llm_with_retry(api_tools)

    # 累加 token 统计
    self._total_input_tokens += response.input_tokens
    self._total_output_tokens += response.output_tokens

    if not response.has_tool_use:
        return response

    # 累加工具调用次数
    self._tool_use_count += len(response.tool_uses)

    results = self._execute_tools(response.tool_uses)
    # ...
```

注意和之前的区别：上一篇只报告 `response.input_tokens`（最后一次 LLM 调用的 token），现在报告 `sub_agent._total_input_tokens`（整个执行过程的累计）。对于一个调用了 5 次 LLM 的子 Agent，差异可能是 10 倍。

然后修改 `AgentTool.call()` 的返回格式：

```python
import uuid
import time

def call(self, input: dict) -> str:
    # 生成唯一 Agent ID
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    start_time = time.monotonic()

    sub_agent = Agent(...)
    response = sub_agent.chat(prompt)

    duration_ms = int((time.monotonic() - start_time) * 1000)

    # 注册子 Agent（后面会解释）
    self._agent_registry[agent_id] = sub_agent

    # 结构化结果
    result_parts = [
        f"[子 Agent: {description}]",
        f"agentId: {agent_id}",
        f"status: completed",
        "",
        content,
        "",
        f"--- 统计: "
        f"turns={sub_agent.turn_count}, "
        f"tool_uses={sub_agent._tool_use_count}, "
        f"duration={duration_ms}ms, "
        f"total_input_tokens={sub_agent._total_input_tokens}, "
        f"total_output_tokens={sub_agent._total_output_tokens} ---",
    ]
    return "\n".join(result_parts)
```

新的返回格式：

```
[子 Agent: 搜索工具文件]
agentId: agent-a1b2c3d4
status: completed

tools/ 目录下有 9 个 Python 文件...

--- 统计: turns=3, tool_uses=5, duration=2340ms, total_input_tokens=3600, total_output_tokens=1200 ---
```

多了什么：
- `agentId: agent-a1b2c3d4` — 唯一标识，用于后续 `send_message`
- `status: completed` — 状态字段
- `tool_uses=5` — 累计工具调用次数
- `duration=2340ms` — 实际耗时
- `total_input/output_tokens` — 累计 token（而非最后一次）

---

## 问题二：子 Agent "用完即弃"

之前的子 Agent 在 `call()` 执行完后，就变成了一个局部变量被垃圾回收。它积累的消息历史、对项目的理解——全部丢失。

如果主 Agent 想追问："你刚才分析 `agent.py` 时提到了一个复杂方法，能详细展开吗？"，只能启动一个全新的子 Agent，从头阅读 `agent.py`。

### 解决：Agent 注册表 + SendMessage 工具

核心思路：**子 Agent 执行完后不丢弃，存到注册表里**。主 Agent 通过 `agentId` 随时找回它继续对话。

#### Agent 注册表

在主 Agent 中维护一个字典：

```python
class Agent:
    def __init__(self, ...):
        # ...
        # 子 Agent 注册表
        self._sub_agent_registry: dict[str, "Agent"] = {}
```

`AgentTool` 和 `SendMessageTool` 在初始化时**共享同一个字典引用**：

```python
if _enable_agent_tool:
    from agent.tools.agent_tool import AgentTool
    from agent.tools.send_message import SendMessageTool

    sub_tools = [t for t in self.tools if t.name not in ("agent", "send_message")]

    agent_tool = AgentTool(
        llm=llm,
        tools=sub_tools,
        agent_registry=self._sub_agent_registry,  # 共享引用
    )
    send_message_tool = SendMessageTool(
        agent_registry=self._sub_agent_registry,  # 同一个字典
    )
    self.tools.append(agent_tool)
    self.tools.append(send_message_tool)
```

`AgentTool.call()` 在执行完子 Agent 后，把它注册到字典中：

```python
self._agent_registry[agent_id] = sub_agent
```

这样 `SendMessageTool` 就能通过 `agent_id` 找到这个子 Agent。

#### SendMessage 工具

新工具，输入 `agent_id` + `message`，输出结构化结果：

```python
class SendMessageTool:
    def __init__(self, agent_registry: dict):
        self._agent_registry = agent_registry

    @property
    def name(self) -> str:
        return "send_message"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "目标子 Agent 的 ID",
                },
                "message": {
                    "type": "string",
                    "description": "要发送给子 Agent 的消息内容",
                },
            },
            "required": ["agent_id", "message"],
        }

    def call(self, input: dict) -> str:
        agent_id = input.get("agent_id", "")
        message = input.get("message", "")

        sub_agent = self._agent_registry.get(agent_id)
        if sub_agent is None:
            available = list(self._agent_registry.keys())
            return f"错误：找不到 agentId='{agent_id}'。可用: {available}"

        # 继续对话（子 Agent 保留了之前的消息历史）
        response = sub_agent.chat(message)

        # 返回结构化结果
        return "\n".join([
            f"[SendMessage → {agent_id}]",
            f"status: completed",
            "",
            response.text or "（未返回文本）",
            "",
            f"--- 统计: turns={sub_agent.turn_count}, ... ---",
        ])
```

关键点：`sub_agent.chat(message)` 调用的是**同一个 Agent 实例**，它保留了之前所有的消息历史。对子 Agent 来说，这就像主 Agent 在跟它"继续聊天"。

#### 典型交互流程

```
用户: "分析 agent/agent.py 的架构，然后告诉我最复杂的方法是哪个"

主 Agent:
  → 调用 agent(prompt="分析 agent/agent.py 的架构")
  → 子 Agent 读取文件、分析、返回架构概述
  → tool_result 包含 agentId: agent-a1b2c3d4

主 Agent（看到结果后，想要更多细节）:
  → 调用 send_message(agent_id="agent-a1b2c3d4", message="哪个方法最复杂？展开说说")
  → 子 Agent 已经有完整的 agent.py 内容在消息历史中
  → 不需要重新读取文件，直接分析回答
  → tool_result: "最复杂的是 stream() 方法，因为..."

主 Agent:
  → 综合两次结果，生成最终回复
```

如果没有 `send_message`，第二步需要启动全新子 Agent，重新读取 `agent.py`——浪费时间和 token。

---

## 问题三：子 Agent 角色固定

之前所有子 Agent 都用同一个 system prompt：

```
你是一个专注执行子任务的 Agent。
你的职责：完整地完成分配给你的任务...
```

但有时候需要不同角色：
- 代码审查：需要严格、关注安全和边界条件
- 文档撰写：需要清晰、面向用户
- 测试生成：需要关注边界情况和覆盖率

### 解决：per-call system prompt

在 `AgentTool.input_schema` 中新增 `system` 可选字段：

```python
@property
def input_schema(self) -> dict:
    return {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", ...},
            "description": {"type": "string", ...},
            "system": {
                "type": "string",
                "description": "自定义 system prompt（可选，覆盖默认）",
            },
        },
        "required": ["prompt"],
    }
```

在 `call()` 中使用：

```python
def call(self, input: dict) -> str:
    custom_system = input.get("system")
    system = custom_system or self._system  # 优先使用调用时指定的

    sub_agent = Agent(
        llm=self._llm,
        tools=self._tools,
        system=system,  # 可能是自定义的
        ...
    )
```

LLM 可以这样调用：

```json
{
  "name": "agent",
  "input": {
    "prompt": "审查 agent/agent.py 中的错误处理",
    "description": "代码审查",
    "system": "你是一个严格的代码审查专家。关注：1) 异常处理是否完整 2) 边界条件 3) 安全漏洞"
  }
}
```

---

## 并发安全性

`AgentTool.is_concurrency_safe()` 仍然返回 `True`——每个子 Agent 是独立实例，并发启动多个子 Agent 完全没问题。

但 `SendMessageTool.is_concurrency_safe()` 返回 `False`：

```python
def is_concurrency_safe(self, input: dict) -> bool:
    # 同一个子 Agent 不应并发接收消息（消息历史会冲突）
    return False
```

原因：如果同时向同一个子 Agent 发两条消息，两次 `sub_agent.chat()` 会并发修改同一个 `messages` 列表，导致消息顺序混乱。

理论上可以做更精细的判断（不同 `agent_id` 可以并发），但保守策略更安全。

---

## 注册表的生命周期

一个需要注意的设计决策：**注册表中的子 Agent 何时清理？**

当前的实现很简单——不主动清理。子 Agent 存在于 `_sub_agent_registry` 字典中，只要主 Agent 存在，所有子 Agent 都会保留在内存中。

这在实践中是合理的：

1. **会话有限**：主 Agent 的生命周期就是一次 CLI 运行或一段对话，不会无限增长
2. **内存开销小**：每个子 Agent 主要存储是 `messages` 列表（几十 KB 级别）
3. **复杂清理不值得**：实现 LRU 淘汰、超时清理等增加大量复杂度，收益很小

如果未来需要长时间运行的场景（如 daemon 模式），可以考虑添加：
- 注册表大小限制
- 子 Agent 超时清理
- 消息历史持久化到磁盘

但目前，简单就是好的。

---

## 与 Claude Code 源码实现的对比

| | Claude Code（TS 版） | 我们的实现（Python 版） |
|---|---|---|
| **结构化输出** | `status`, `agentId`, `content`, `totalToolUseCount`, `totalDurationMs`, `totalTokens`, `usage`, `worktreePath` | `status`, `agentId`, `content`, `turns`, `tool_uses`, `duration`, `total_input/output_tokens` |
| **SendMessage** | 支持（可向任何 teammate 发消息） | 支持（向子 Agent 发送后续消息） |
| **Agent 注册** | 全局注册表 + teammates 概念 | 主 Agent 实例级注册表 |
| **System prompt** | 按 agent type 不同 + fork 模式 | 默认 + per-call 覆盖 |
| **任务通知** | `<task-notification>` XML 异步通知 | 同步返回结果 |
| **Coordinator 模式** | 专门的 system prompt + 4 阶段流程 | 无（LLM 自行决定工作流程） |
| **Worktree 隔离** | git worktree 文件系统隔离 | 无 |

Claude Code 的实现显著更复杂：
- **teammates 概念**：不只是父→子通信，是多个平级 Agent 之间的对等通信
- **异步通知**：子 Agent 在后台运行，通过 `<task-notification>` XML 向父 Agent 推送进度
- **Coordinator 模式**：专门的"协调员" Agent，有 4 阶段工作流（Research→Synthesis→Implementation→Verification）
- **Worktree 隔离**：每个子 Agent 在独立的 git worktree 中工作，避免文件冲突

我们的实现覆盖了最核心的需求：**子 Agent 可以被识别、可以被追踪、可以继续对话**。更高级的功能（异步通知、Coordinator 模式）留待后续实现。

---

## 设计哲学：LLM 作为编排器

本篇一个重要的设计决策是：**不做编程式的任务分解和结果聚合**。

很多 Agent 框架会这样做：

```python
# 编程式编排（我们没有这样做）
sub_tasks = decompose(main_task)  # 程序化分解任务
results = [agent.run(task) for task in sub_tasks]  # 并行执行
final = aggregate(results)  # 程序化聚合结果
```

我们的做法是把编排权交给 LLM：

```python
# LLM 编排（我们的做法）
response = llm.chat(messages, tools=[agent_tool, send_message_tool, ...])
# LLM 自己决定：
# - 是否需要启动子 Agent
# - 启动几个子 Agent
# - 是否需要追问子 Agent
# - 如何综合多个子 Agent 的结果
```

这个选择和 Claude Code 的设计一致。Claude Code 的源码中也没有硬编码的任务分解框架——它提供了工具（`Agent`、`SendMessage`），让 LLM 自己决定如何使用。

好处是灵活性：LLM 可以根据任务特点动态调整策略，不受编程式框架的限制。代价是可预测性：你无法确切知道 LLM 会如何分解任务。

在实践中，这种灵活性通常更有价值。固定的分解框架很难覆盖所有场景，而 LLM 的判断力通常足够好。

---

## 小结

本篇在上一篇的基础上增加了三个能力：

| 能力 | 实现方式 | 解决的问题 |
|---|---|---|
| 结构化输出 | `agentId` + `status` + 累计统计 | 不知道子 Agent 的身份和开销 |
| SendMessage | `send_message(agent_id, message)` | 无法追问已完成的子 Agent |
| 自定义 system | `agent(prompt, system="...")` | 所有子 Agent 用同一个角色 |

核心代码变化：

```
agent/agent.py
  + _total_input_tokens, _total_output_tokens, _tool_use_count  # 统计追踪
  + _sub_agent_registry: dict[str, Agent]                       # 注册表
  + SendMessageTool 自动注入                                     # 新工具

agent/tools/agent_tool.py
  + uuid + time 导入                                             # 生成 ID 和计时
  + agent_registry 参数                                          # 共享注册表
  + system 字段（input_schema）                                  # per-call system prompt
  + 结构化返回格式                                               # agentId + status + 统计

agent/tools/send_message.py (新文件)
  + SendMessageTool 类                                           # 向子 Agent 发送消息
  + 注册表查找 + 错误处理                                        # agent_id 验证
  + 结构化返回格式                                               # 与 AgentTool 一致
```

下一篇将实现**并发多 Agent**——让主 Agent 同时启动多个子 Agent 进行 fan-out / fan-in 模式的并行处理。
