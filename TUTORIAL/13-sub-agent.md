# 从零实现 Coding Agent（十三）：子 Agent

到目前为止，我们的 Agent 是一个单循环系统：收到用户输入 → 调用 LLM → 执行工具 → 回传结果 → 继续。所有任务都在同一个循环中完成，消息历史越来越长。

当任务复杂到需要"先做 A，再做 B，然后综合 AB 的结果做 C"时，单循环 Agent 的问题就暴露了：

1. **消息历史膨胀**：每个子任务的工具调用和结果都堆在同一个历史里，很快撑满上下文窗口
2. **注意力分散**：LLM 需要在一长串消息中找到相关内容，容易遗漏细节
3. **无法并行**：子任务之间可能没有依赖，但单循环只能串行处理

解决方案很直接：**让主 Agent 启动独立的子 Agent 来处理子任务**。每个子 Agent 有自己的消息历史和 tool use 循环，完成后把结果摘要返回给主 Agent。

---

## 核心设计：agent 作为一个工具

子 Agent 的实现方式出人意料地简单——它就是一个**工具**。

```
主 Agent 的工具列表：
  [read, write, edit, bash, glob, grep, get_current_time, agent]
                                                           ^^^^^
                                                     这是一个工具
```

当 LLM 认为需要委托子任务时，它会像调用 `read` 或 `bash` 一样调用 `agent` 工具：

```json
{
  "type": "tool_use",
  "name": "agent",
  "input": {
    "prompt": "搜索 agent/tools/ 目录下所有 Python 文件，列出每个文件的功能",
    "description": "搜索工具文件"
  }
}
```

`agent` 工具的 `call()` 方法会创建一个全新的 `Agent` 实例，执行这个 prompt，然后将结果作为 `tool_result` 返回。对主 Agent 来说，子 Agent 就是一个"执行时间比较长的工具"。

这个设计的优雅之处在于：**不需要修改任何已有的 Agent 核心逻辑**。tool use 循环、消息格式、并发执行——一切照旧，子 Agent 只是多了一个工具选项。

---

## 实现：AgentTool

### 为什么不放在 BUILTIN_TOOLS 里？

之前的工具（read、bash、glob 等）都在 `BUILTIN_TOOLS` 列表中，模块加载时就创建好实例。但 `AgentTool` 不行——它需要运行时的 LLM 实例和工具列表：

```python
# 这些工具可以在模块加载时创建
BUILTIN_TOOLS = [ReadTool(), BashTool(), GlobTool(), ...]

# AgentTool 不行——它需要知道用哪个 LLM 和哪些工具
# AgentTool(llm=???, tools=???)  ← 这些信息只有在 Agent 初始化时才知道
```

所以 `AgentTool` 由 `Agent` 在初始化时自动创建和注入。

### Agent.__init__ 的变化

```python
class Agent:
    def __init__(self, llm, tools=None, ..., _enable_agent_tool=True):
        self.tools = tools or []
        # ...

        # 自动添加子 Agent 工具
        if _enable_agent_tool:
            from agent.tools.agent_tool import AgentTool
            sub_tools = [t for t in self.tools if t.name != "agent"]
            agent_tool = AgentTool(
                llm=llm,
                tools=sub_tools,
                max_turns=min(max_turns, 10),
                enable_budget=enable_budget,
                enable_permission=enable_permission,
            )
            self.tools.append(agent_tool)
```

几个关键点：

1. **`sub_tools` 排除 agent 工具本身**：`[t for t in self.tools if t.name != "agent"]`，这是**防止递归**的核心。如果子 Agent 也有 `agent` 工具，它就可以无限嵌套启动子子 Agent。

2. **`_enable_agent_tool=True`**：带下划线前缀表示这是内部参数。子 Agent 创建时传 `False`，所以子 Agent 不会再自动添加 `agent` 工具。

3. **`max_turns=min(max_turns, 10)`**：子 Agent 的 turn 限制更保守。即使父 Agent 的 max_turns 是 20，子 Agent 也不超过 10。

### AgentTool.call()

```python
# agent/tools/agent_tool.py

class AgentTool:
    def __init__(self, llm, tools, system=None, max_turns=10, ...):
        self._llm = llm
        self._tools = tools
        self._system = system or _SUB_AGENT_SYSTEM
        self._max_turns = max_turns
        # ...

    def call(self, input: dict) -> str:
        from agent.agent import Agent  # 延迟导入，避免循环引用

        prompt = input.get("prompt", "")
        description = input.get("description", "子任务")

        # 创建全新的 Agent 实例（独立消息历史）
        sub_agent = Agent(
            llm=self._llm,
            tools=self._tools,
            system=self._system,
            max_turns=self._max_turns,
            enable_compact=False,      # 子任务通常较短
            _enable_agent_tool=False,  # 防止递归
        )

        response = sub_agent.chat(prompt)

        # 格式化结果
        result_parts = [f"[子 Agent: {description}]"]
        if response.text:
            result_parts.append(response.text)

        result_parts.append(
            f"\n--- 子 Agent 统计: "
            f"turns={sub_agent.turn_count}, "
            f"messages={len(sub_agent.messages)}, "
            f"input_tokens={response.input_tokens}, "
            f"output_tokens={response.output_tokens} ---"
        )
        return "\n".join(result_parts)
```

**延迟导入** `from agent.agent import Agent` 放在 `call()` 而非模块顶层，是因为 `agent.py` 已经导入了 `agent.tools`，如果 `agent_tool.py` 也在顶层导入 `agent.py`，就形成了循环引用。放在函数内部，只在实际调用时触发导入，此时两个模块都已完成初始化。

### 子 Agent 的 system prompt

```python
_SUB_AGENT_SYSTEM = """你是一个专注执行子任务的 Agent。

你的职责：
- 完整地完成分配给你的任务
- 使用可用的工具来获取信息和执行操作
- 完成后给出清晰、简洁的结果摘要

注意：
- 你是一个子 Agent，由主 Agent 启动来处理特定子任务
- 专注于手头的任务，不要偏离
- 完成任务后直接汇报结果，不需要询问后续步骤"""
```

子 Agent 用独立的 system prompt 而非继承父 Agent 的，原因是：
- 父 Agent 的 system prompt 包含项目规范、AGENTS.md 内容等，可能很长
- 子 Agent 只需要聚焦完成一个具体任务
- 更短的 system prompt = 更多的上下文空间留给工具输出

---

## 并发安全：多个子 Agent 同时运行

`AgentTool.is_concurrency_safe()` 返回 `True`：

```python
def is_concurrency_safe(self, input: dict) -> bool:
    return True  # 每个子 Agent 是完全独立的实例
```

这意味着当 LLM 一次返回多个 `agent` 工具调用时，前面实现的并发执行机制会自动将它们并行运行：

```
LLM 返回：
  agent(prompt="搜索 agent/tools/ 目录结构")
  agent(prompt="搜索 agent/llm/ 目录结构")

分区结果：[[0, 1]]  ← 同一批次，并发执行

→ ThreadPoolExecutor 同时启动两个子 Agent
→ 两个独立的 Agent 实例，各自维护消息历史
→ 两个独立的 tool use 循环，可以同时调用 read、glob 等工具
→ 结果按原始顺序返回给主 Agent
```

每个子 Agent 是 `Agent()` 的新实例，拥有独立的 `self.messages`，不共享任何可变状态。这就是为什么并发执行是安全的。

---

## 执行流程全景

```
用户: "分析 agent/tools/ 和 agent/llm/ 两个目录的代码结构"

主 Agent:
  messages: [user("分析...")]
     ↓
  LLM 决定启动两个子 Agent
     ↓
  tool_use: agent(prompt="分析 tools/...", description="分析工具目录")
  tool_use: agent(prompt="分析 llm/...",   description="分析 LLM 目录")
     ↓
  并发执行（两个都是 concurrency_safe）
     ↓
  ┌─ 子 Agent A ──────────────────────┐  ┌─ 子 Agent B ──────────────────────┐
  │ messages: [user("分析 tools/...")]│  │ messages: [user("分析 llm/...")] │
  │ → LLM: glob(agent/tools/*.py)     │  │ → LLM: glob(agent/llm/*.py)      │
  │ → tool_result: 8 个文件           │  │ → tool_result: 4 个文件          │
  │ → LLM: read(base.py)              │  │ → LLM: read(base.py)             │
  │ → LLM: read(bash.py)              │  │ → LLM: read(anthropic_llm.py)    │
  │ → ...                             │  │ → ...                            │
  │ → 最终回复: "tools/ 目录下有..."  │  │ → 最终回复: "llm/ 目录下有..."   │
  └────────────────────────────────────┘  └────────────────────────────────────┘
     ↓                                       ↓
  tool_result: "[子 Agent: 分析工具目录]    tool_result: "[子 Agent: 分析 LLM 目录]
               tools/ 目录下有..."                      llm/ 目录下有..."
     ↓
  主 Agent 综合两个结果，生成最终回复
     ↓
  "agent/tools/ 包含 8 个工具文件... agent/llm/ 包含 4 个文件..."
```

注意主 Agent 的消息历史只有：用户输入 → assistant(tool_use) → tool_result(子 Agent 摘要) → assistant(最终回复)。子 Agent 内部的 10+ 条消息（glob 结果、read 内容等）**不会污染主 Agent 的消息历史**。

---

## 与 Claude Code 源码实现的对比

| | Claude Code（TS 版） | 我们的实现（Python 版） |
|---|---|---|
| **工具名** | `Agent`（别名 `Task`） | `agent` |
| **输入参数** | prompt, description, subagent_type, model, run_in_background | prompt, description |
| **Agent 类型** | 多种内置类型（general-purpose, explore, plan）+ 自定义 | 单一类型（通用） |
| **System prompt** | 按类型不同；fork 模式继承父 Agent 的完整 prompt | 固定的子任务专用 prompt |
| **工具集** | 按类型过滤；fork 模式继承父 Agent 的完整工具集 | 继承父 Agent 工具集（减去 agent 自身） |
| **嵌套** | 外部用户禁止；内部用户允许 | 禁止（`_enable_agent_tool=False`） |
| **异步支持** | 支持后台运行 + 进度通知 | 仅同步 |
| **并发** | 是（独立的 ToolUseContext） | 是（独立的 Agent 实例） |
| **错误级联** | Bash 错误取消兄弟 Agent | 无级联 |

Claude Code 的实现复杂得多——支持多种 Agent 类型、异步后台运行、fork 模式（子 Agent 继承父 Agent 的完整上下文以共享 prompt cache）、团队协作等。我们的实现是最精简的核心：**一个独立的 Agent 实例执行一个子任务**。

最大的简化是省略了 Agent 类型系统。Claude Code 有 `general-purpose`（通用）、`explore`（只读搜索）、`plan`（架构设计）等类型，每种有不同的 system prompt、工具集和模型选择。我们用一个通用的子 Agent 覆盖所有场景，够用且容易理解。

---

## 一个设计权衡：为什么禁止嵌套？

子 Agent 不能再启动子 Agent（`_enable_agent_tool=False`）。这是故意的限制：

1. **深度难以控制**：如果允许嵌套，理论上可以无限递归。即使有 max_turns 保护，3 层嵌套 × 10 turns × 每 turn 一次 LLM 调用 = 300 次 API 调用，费用惊人。

2. **调试困难**：嵌套越深，追踪哪个 Agent 在干什么就越难。单层子 Agent 已经能覆盖绝大多数场景。

3. **收益递减**：子 Agent 的价值在于隔离消息历史和并行执行。子子 Agent 带来的额外收益很小，但复杂度急剧增加。

如果真有需要多级分解的场景，可以让主 Agent 分两轮启动子 Agent：第一轮子 Agent 完成初步分析，主 Agent 基于结果在第二轮启动新的子 Agent 做深入分析。效果相似，但控制权始终在主 Agent 手中。

---

## 小结

子 Agent 的本质是：**把 Agent 自身封装成一个工具**。

```
AgentTool.call(prompt):
    sub_agent = Agent(llm, tools, _enable_agent_tool=False)
    response = sub_agent.chat(prompt)
    return response.text
```

就这么简单。复杂的部分是设计决策，不是代码：

| 决策 | 选择 | 原因 |
|---|---|---|
| 实现方式 | 工具 | 复用已有的 tool use 循环，不改核心逻辑 |
| 注册方式 | Agent 自动注入 | 需要运行时 LLM 配置，不能静态注册 |
| 递归 | 禁止 | 防止失控，单层已覆盖绝大多数场景 |
| 并发 | 安全 | 每个子 Agent 是独立实例，无共享状态 |
| System prompt | 独立 | 子 Agent 聚焦任务，不需要父 Agent 的完整上下文 |
| Max turns | 更保守 | 子任务通常不需要太多轮次 |

下一篇将实现 **Agent 间通信**——让主 Agent 和子 Agent 之间传递更结构化的信息。
