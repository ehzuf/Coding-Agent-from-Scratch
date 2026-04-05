# 从零实现 Coding Agent（十五）：Coordinator 模式

在前面几篇中，我们实现了子 Agent（可以委托子任务）、Agent 间通信（可以追问子 Agent）、并发工具执行（多个工具同时运行）。这些能力组合在一起，已经让 LLM 可以同时启动多个子 Agent 并行处理子任务。

但有一个问题：**主 Agent 既要亲自干活，又要管理子 Agent**。就像一个工程师既写代码又当项目经理，两个角色不断切换，效率很低。

本篇引入 **Coordinator 模式**：让主 Agent 专职做"协调者"，只负责拆解任务、分配 Worker、综合结果。所有具体工作都交给 Worker（子 Agent）。

这对应 Claude Code 源码中的 `coordinator/coordinatorMode.ts`。

---

## 为什么需要 Coordinator 模式？

先看一个实际场景。用户说："分析 `agent/tools/` 和 `agent/llm/` 两个目录的代码结构。"

**普通模式下**，Agent 可能会：

```
Agent: 好的，我先看 tools 目录
  → [glob] 查找 agent/tools/*.py
  → [read] 读取 base.py
  → [read] 读取 bash.py
  → [read] 读取 read.py
  → ... 逐个读取 9 个文件
Agent: 现在看 llm 目录
  → [glob] 查找 agent/llm/*.py
  → [read] 读取 base.py
  → ... 逐个读取 4 个文件
Agent: 综合分析如下...
```

全程串行，tools 目录分析完才开始 llm 目录。

**Coordinator 模式下**：

```
Coordinator: 我来分配两个 Worker 并行分析。
  → [agent] Worker 1: "分析 agent/tools/ 的代码结构"
  → [agent] Worker 2: "分析 agent/llm/ 的代码结构"
    （两个 Worker 同时执行，各自独立使用 glob、read 等工具）
Worker 1 完成: <task-notification>...</task-notification>
Worker 2 完成: <task-notification>...</task-notification>
Coordinator: 综合两个 Worker 的分析结果如下...
```

两个方向的分析并行进行，Coordinator 只做最后的综合。这就是 **fan-out / fan-in** 模式。

---

## 核心洞察：系统提示决定行为

实现 Coordinator 模式不需要修改任何 Agent 核心逻辑——**换一个系统提示就够了**。

同一个 LLM，给它"你是一个执行者"的指令，它会亲自调用 read、bash 等工具。给它"你是一个协调者，把任务分配给 Worker"的指令，它会调用 agent 工具启动子 Agent。

这是 LLM agent 架构的一个优雅特性：**角色切换只需要改 system prompt**。

---

## 实现：coordinator.py

新建 `agent/coordinator.py`，包含三个核心组件。

### 1. Coordinator 系统提示

```python
COORDINATOR_SYSTEM_PROMPT = """你是一个 AI 编程助手，负责协调多个 Worker 完成软件工程任务。

## 1. 你的角色

你是 **Coordinator（协调者）**。你的职责是：
- 理解用户目标，将任务拆解为可并行的子任务
- 启动 Worker（子 Agent）执行具体工作
- 综合 Worker 的结果，向用户汇报

每条消息都是给用户的。Worker 的结果是内部信号，不是对话伙伴——
不要感谢或确认 Worker，直接总结信息给用户。

## 2. 你的工具

- **agent** — 启动一个新的 Worker
- **send_message** — 继续一个已有的 Worker

## 3. 任务工作流

| 阶段 | 执行者 | 目的 |
|------|--------|------|
| 调研 | Worker（并行） | 探索代码库，理解问题 |
| 综合 | **你（Coordinator）** | 阅读调研结果，制定实施方案 |
| 实施 | Worker | 按照方案执行修改 |
| 验证 | Worker | 测试修改是否正确 |

## 4. 并发是你的优势

**尽量并行启动 Worker。** 不要串行化可以并行的工作。
要并行启动 Worker，在一次回复中调用多个 agent 工具。

## 5. 编写 Worker 指令

**Worker 看不到你和用户的对话。** 每个指令必须自包含。"""
```

对比 Claude Code 的 `getCoordinatorSystemPrompt()`，我们精简了很多（去掉了 TaskStop、subscribe_pr_activity 等高级功能），但保留了核心的四阶段工作流和指令编写规范。

关键设计：系统提示明确告诉 LLM "你是协调者，不要自己做"，并且提供了清晰的 4 阶段工作流模型（Research → Synthesis → Implementation → Verification）。

### 2. Worker 系统提示

```python
WORKER_SYSTEM_PROMPT = """你是一个执行具体任务的 Worker。

规则：
1. 严格完成分配给你的任务，不要偏离
2. 使用工具直接执行，不要闲聊
3. 完成后给出简洁的结果报告
4. 如果任务是调研，只报告发现，不要修改文件
5. 如果任务是实施，报告修改的文件和关键变更"""
```

Worker 的提示很短，因为它只需要执行一件事。越简洁越聚焦。

### 3. 结构化任务通知

```python
def format_task_notification(
    agent_id: str,
    description: str,
    status: str,
    content: str,
    turns: int = 0,
    tool_uses: int = 0,
    duration_ms: int = 0,
    total_input_tokens: int = 0,
    total_output_tokens: int = 0,
) -> str:
    """将子 Agent 结果格式化为 <task-notification> XML。"""
    parts = [
        "<task-notification>",
        f"<task-id>{agent_id}</task-id>",
        f"<description>{description}</description>",
        f"<status>{status}</status>",
        f"<result>\n{content}\n</result>",
        "<usage>",
        f"  <turns>{turns}</turns>",
        f"  <tool_uses>{tool_uses}</tool_uses>",
        f"  <duration_ms>{duration_ms}</duration_ms>",
        f"  <total_input_tokens>{total_input_tokens}</total_input_tokens>",
        f"  <total_output_tokens>{total_output_tokens}</total_output_tokens>",
        "</usage>",
        "</task-notification>",
    ]
    return "\n".join(parts)
```

为什么用 XML 而不是 JSON？因为 **LLM 对 XML 标签的解析更稳定**。Claude Code 也是用 `<task-notification>` XML 格式。在 tool_result 中嵌入 JSON 容易被 LLM 误解析（JSON 的引号和转义与自然语言混淆），XML 的开闭标签更明确。

> **教学简化**：我们的 XML schema 与 Claude Code 的略有不同（字段名和嵌套结构有差异），但核心思路一致：用结构化 XML 让 Coordinator 能可靠地解析 Worker 返回的结果。

输出示例：

```xml
<task-notification>
<task-id>agent-a1b2c3d4</task-id>
<description>分析 tools 目录</description>
<status>completed</status>
<result>
agent/tools/ 目录下有 9 个 Python 文件：
- base.py: Tool 抽象基类...
- bash.py: Bash 命令执行工具...
</result>
<usage>
  <turns>3</turns>
  <tool_uses>5</tool_uses>
  <duration_ms>2340</duration_ms>
  <total_input_tokens>1200</total_input_tokens>
  <total_output_tokens>450</total_output_tokens>
</usage>
</task-notification>
```

---

## 改造 AgentTool：双模式输出

`AgentTool` 需要根据是否处于 Coordinator 模式选择不同的输出格式和 Worker 提示。

### 新增 coordinator_mode 参数

```python
class AgentTool:
    def __init__(self, llm, tools, system=None, max_turns=10,
                 enable_budget=True, enable_permission=False,
                 agent_registry=None, coordinator_mode=False):
        # ...
        self._coordinator_mode = coordinator_mode
```

### system prompt 三层优先级

```python
def call(self, input: dict) -> str:
    custom_system = input.get("system")

    # 优先级：调用时指定 > Coordinator Worker 默认 > 普通默认
    if custom_system:
        system = custom_system
    elif self._coordinator_mode:
        system = WORKER_SYSTEM_PROMPT
    else:
        system = self._system

    sub_agent = Agent(llm=self._llm, tools=self._tools, system=system, ...)
```

三层优先级：
1. `input.get("system")` — 调用时指定（最高）
2. `WORKER_SYSTEM_PROMPT` — Coordinator 模式的 Worker 默认
3. `self._system`（即 `_SUB_AGENT_SYSTEM`）— 普通模式的子 Agent 默认

### 双模式返回格式

```python
    content = response.text or "（子 Agent 未返回文本结果）"

    # Coordinator 模式：<task-notification> XML
    if self._coordinator_mode:
        return format_task_notification(
            agent_id=agent_id,
            description=description,
            status="completed",
            content=content,
            turns=sub_agent.turn_count,
            tool_uses=sub_agent._tool_use_count,
            duration_ms=duration_ms,
            total_input_tokens=sub_agent._total_input_tokens,
            total_output_tokens=sub_agent._total_output_tokens,
        )
    else:
        # 普通模式：纯文本格式（与之前一致）
        result_parts = [
            f"[子 Agent: {description}]",
            f"agentId: {agent_id}",
            # ...
        ]
        return "\n".join(result_parts)
```

普通模式的输出格式保持不变，Coordinator 模式使用 XML 格式。两种格式都包含 `agentId`，都可以被 `send_message` 继续对话。

---

## 改造 Agent：注入 Coordinator 系统提示

Agent 初始化时根据 `coordinator_mode` 决定是否使用 Coordinator 系统提示：

```python
from agent.coordinator import (
    COORDINATOR_SYSTEM_PROMPT,
    build_coordinator_context,
)

class Agent:
    def __init__(self, ..., coordinator_mode: bool = False, ...):
        self.coordinator_mode = coordinator_mode

        # Coordinator 模式：合并系统提示
        if coordinator_mode:
            coordinator_ctx = build_coordinator_context(
                [t.name for t in self.tools]
            )
            base = COORDINATOR_SYSTEM_PROMPT + coordinator_ctx
            self.system = base + "\n\n" + system if system else base
        else:
            self.system = system
```

`build_coordinator_context()` 会注入 Worker 可用的工具列表，帮助 Coordinator 理解 Worker 的能力边界：

```python
def build_coordinator_context(tool_names: list[str]) -> str:
    worker_tools = ", ".join(sorted(tool_names))
    return f"\nWorker 可用的工具: {worker_tools}"
```

然后在创建 `AgentTool` 时传递 `coordinator_mode`：

```python
        if _enable_agent_tool:
            agent_tool = AgentTool(
                llm=llm,
                tools=sub_tools,
                max_turns=min(max_turns, 10),
                # ...
                coordinator_mode=coordinator_mode,  # 传递模式
            )
```

---

## CLI 和配置

### 命令行参数

```python
parser.add_argument(
    "--coordinator",
    action="store_true",
    help="启用 Coordinator 模式（主 Agent 编排子 Agent 并行执行任务）",
)
```

### 环境变量

```python
# config.py
if os.environ.get("AGENT_COORDINATOR"):
    config.coordinator_mode = True
```

### 使用方式

```bash
# 普通模式（默认）
python -m agent "分析项目结构"

# Coordinator 模式
python -m agent --coordinator "分析项目结构"

# 环境变量
export AGENT_COORDINATOR=1
python -m agent "分析项目结构"
```

---

## 并发执行：复用已有机制

一个关键设计：**Coordinator 模式不需要新的并发机制**。

前面第十二篇实现的并发工具执行已经覆盖了这个场景：

1. `AgentTool.is_concurrency_safe()` 返回 `True`（每个子 Agent 是独立实例）
2. Coordinator 在一次回复中调用多个 `agent` 工具
3. `Agent._execute_tools()` 检测到多个并发安全的工具调用
4. `ThreadPoolExecutor` 并行执行多个子 Agent

```
Coordinator 响应:
  tool_use: agent({prompt: "分析 tools/", description: "分析工具目录"})
  tool_use: agent({prompt: "分析 llm/", description: "分析 LLM 目录"})

_partition_tool_calls:
  安全性: [True, True]  → 同一批次

_execute_tools:
  ThreadPoolExecutor(max_workers=2):
    线程 1: AgentTool.call({"prompt": "分析 tools/", ...})
    线程 2: AgentTool.call({"prompt": "分析 llm/", ...})
  两个子 Agent 同时执行，各自独立调用 glob、read 等工具
```

这就是架构设计的好处——**新功能几乎零成本地建立在已有机制之上**。并发工具执行（第十二篇）+ 子 Agent 工具（第十三篇）= 并发多 Agent（本篇）。

---

## 与 Claude Code 源码实现的对比

| | Claude Code（TS 版） | 我们的实现（Python 版） |
|---|---|---|
| **Coordinator 提示** | 详细的 6 节提示（角色、工具、Worker、工作流、指令编写、示例） | 精简的 5 节提示（角色、工具、工作流、并发、指令编写） |
| **Worker 提示** | fork 模式（继承父对话）+ worker 模式（独立对话） | 独立 Worker 提示，调用时可覆盖 |
| **结果格式** | `<task-notification>` XML 异步通知 | `<task-notification>` XML 同步返回 |
| **并发机制** | 异步（async/await + AbortController） | 同步并发（ThreadPoolExecutor） |
| **任务生命周期** | running → completed/failed/killed | completed（同步，无中间状态） |
| **TaskStop** | 支持中止运行中的 Worker | 不支持（同步执行无法中止） |
| **Worktree 隔离** | git worktree 文件系统隔离 | 无（Worker 共享工作目录） |
| **Scratchpad** | 跨 Worker 共享的临时目录 | 无 |

核心差异在于 **同步 vs 异步**。Claude Code 的 Worker 是真正的后台异步任务（Node.js async），可以在 Worker 运行过程中继续和用户对话。我们的实现是同步并发——多个 Worker 同时启动，但主 Agent 必须等所有 Worker 完成才能继续。

对于大多数场景这已经足够。真正的异步执行需要更复杂的事件循环和状态管理。

---

## 设计哲学：System Prompt 即架构

本篇最重要的启示是：**Agent 的角色和行为模式完全由 system prompt 定义**。

```
同一个 LLM + 同一套工具

System prompt = "你是编程助手"      → 亲自写代码
System prompt = "你是协调者"        → 分配任务给 Worker
System prompt = "你是代码审查者"    → 只读分析不修改
System prompt = "你是测试工程师"    → 关注测试覆盖率
```

这意味着 Agent 架构的核心不是代码框架，而是 **系统提示的设计**。一个好的 system prompt 能让通用 Agent 变成专业工具。

Claude Code 的 Coordinator 模式本质上就是一个精心设计的 system prompt + 结构化的 Worker 返回格式。没有新的 API，没有新的协议，只是"告诉 LLM 它是谁、它该怎么做"。

---

## 小结

本篇在前几篇的基础上增加了 Coordinator 模式：

| 组件 | 文件 | 作用 |
|---|---|---|
| Coordinator 提示 | `agent/coordinator.py` | 指导 LLM 扮演协调者角色 |
| Worker 提示 | `agent/coordinator.py` | 指导子 Agent 聚焦执行 |
| task-notification | `agent/coordinator.py` | XML 格式的结构化结果 |
| 模式切换 | `agent/agent.py` | `coordinator_mode` 参数 |
| CLI 支持 | `agent/__main__.py` | `--coordinator` 标志 |

核心代码变化：

```
agent/coordinator.py (新文件)
  + COORDINATOR_SYSTEM_PROMPT     # Coordinator 角色提示
  + WORKER_SYSTEM_PROMPT          # Worker 角色提示
  + build_coordinator_context()   # Worker 工具信息
  + format_task_notification()    # XML 结构化通知

agent/tools/agent_tool.py
  + coordinator_mode 参数         # 切换输出格式
  + 三层 system prompt 优先级    # 调用时 > Worker 默认 > 普通默认
  + format_task_notification()   # Coordinator 模式用 XML 输出

agent/agent.py
  + coordinator_mode 参数         # 是否启用 Coordinator 模式
  + Coordinator 系统提示合并      # 自动注入角色提示和工具上下文

agent/config.py
  + coordinator_mode 配置         # 环境变量 AGENT_COORDINATOR

agent/__main__.py
  + --coordinator CLI 参数        # 命令行启用
```

下一篇将实现**会话持久化**——把消息历史序列化到磁盘，支持 `--resume` 从中断处继续。
