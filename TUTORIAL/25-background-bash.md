# 从零实现 Coding Agent（二十五）：后台 Bash 命令

前面第 05 篇实现的 `bash` 工具是同步阻塞的——`subprocess.run(..., timeout=120)` 会一直挂在那里，直到命令返回或超时。对于 `ls`、`cat` 这类瞬时命令这没问题，但面对下面这类**长任务**就露馅了：

```
bash("npm run dev")        # 永远不返回，开发服务器持续运行
bash("pytest --watch")     # 文件变动就重跑，也是常驻
bash("python train.py")    # 可能跑几小时
```

一旦进入这种任务，整个 Agent 对话都被堵死——用户想追问、想干别的，都得先等它或者手动杀进程。

这一篇要解决的问题是：**把这类长任务扔到后台继续跑，让 Agent 对话继续往前走；任务真正结束时，再自然地把结果送回 Agent**——中间不打断用户。

整个实现参考 Claude Code 的 `BashTool` + `LocalShellTask` + `messageQueueManager` 三层设计，我们在基础版 [BashTool](../agent/tools/bash.py) 之上做一个简化落地。

---

## 核心问题拆成五件事

这一个功能其实要同时解决五件互相独立的事：

| 子问题 | 谁负责 | 关键数据结构 |
|---|---|---|
| 长任务不阻塞 Agent | 改造后的 `bash` 工具 | `run_in_background` 参数 + 后台进程句柄 |
| 输出不丢、能回读 | 一个独立的 `TaskOutput` | 环形缓冲 + 磁盘文件 |
| 已启动的任务能查询、能 kill | 一张全局任务表 | `TaskRegistry`（id → task 状态） |
| 完成后告诉 Agent 但不打断用户 | 通知队列 + query 循环搭车 | `NotificationQueue`（全局数组） |
| 用户沉默时也能把通知送到 LLM | Agent 主动调用的 `SleepTool` | `has_notifications_for` + `sleepRan` 开关 |

五件事解耦是关键——后面会看到，**通知机制**这条线如果设计错了，就会出现用户正在打字时 Agent 突然"抢话"的糟糕体验；而用户不开口时后台通知会死在队列里出不来。两者合起来才是完整答案。

---

## 改造 Bash 工具：加一个 run_in_background 开关

先从工具入口开始。给 `bash` 工具的 `input_schema` 加一个可选布尔参数：

```python
# agent/tools/bash.py

@property
def input_schema(self) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {"type": "number", "description": "超时时间（毫秒），默认 120000"},
            "cwd": {"type": "string", "description": "工作目录"},
            "run_in_background": {
                "type": "boolean",
                "description": "设为 true 时立即把任务扔到后台并返回 task_id，"
                               "命令继续在后台运行。用 bash_output(task_id) 查看最新输出。",
            },
        },
        "required": ["command"],
    }
```

这是一个"**显式后台化**"入口——LLM 看到 `npm run dev` 这种命令时，应该自己决定加上 `run_in_background: true`。工具内部分叉：

```python
def call(self, input: dict[str, Any]) -> str:
    command = input.get("command", "")
    if input.get("run_in_background"):
        return self._start_background(command, input.get("cwd"))
    # 原来的同步执行分支不变
    return self._run_foreground(command, input)
```

后台分支不等进程结束，启动后立刻返回一个 `task_id`：

```python
def _start_background(self, command: str, cwd: str | None) -> str:
    task = background_registry.spawn(command, cwd=cwd)
    return (
        f"后台任务已启动\n"
        f"task_id: {task.id}\n"
        f"command: {command}\n"
        f"提示：用 bash_output 工具查看实时输出，用 kill_bash 终止任务。"
    )
```

对 LLM 来说，这就是一次普通的工具调用——收到 `task_id` 后就可以继续下一步思考了，不会被进程挂住。

---

## 后台进程：BackgroundShellCommand

进程这一层要做三件事：启动、托管输出、支持被 kill。参考 Claude Code 的 `ShellCommand` 做一个简化版：

```python
# agent/tools/background/shell.py

import subprocess
import threading
import uuid
from pathlib import Path
from typing import Literal
from collections import deque

TaskStatus = Literal["running", "completed", "failed", "killed"]

class BackgroundShellCommand:
    """一个后台运行的 shell 进程，带输出落盘 + 环形缓冲。"""

    _BUFFER_LINES = 1000
    _OUTPUT_DIR = Path.home() / ".coding-agent" / "bg_tasks"

    def __init__(self, command: str, cwd: str | None = None):
        self.id = f"bg-{uuid.uuid4().hex[:8]}"
        self.command = command
        self.cwd = cwd
        self.status: TaskStatus = "running"
        self.exit_code: int | None = None

        self._OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.output_file = self._OUTPUT_DIR / f"{self.id}.log"

        # 1000 行环形缓冲，内存里随手可读的"尾部"
        self._tail: deque[str] = deque(maxlen=self._BUFFER_LINES)
        self._lock = threading.Lock()

        # 关键：stdout/stderr 同时写到磁盘文件
        self._fh = self.output_file.open("w", buffering=1)  # 行缓冲
        self._proc = subprocess.Popen(
            ["bash", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
        )
        # 后台线程负责把管道里的输出持续搬运
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            with self._lock:
                self._tail.append(line)
                self._fh.write(line)
        rc = self._proc.wait()
        self._fh.close()
        with self._lock:
            if self.status == "running":
                self.status = "completed" if rc == 0 else "failed"
                self.exit_code = rc
        # 通知队列：任务结束了
        on_task_finished(self)

    def read_tail(self) -> str:
        with self._lock:
            return "".join(self._tail)

    def kill(self) -> None:
        if self.status == "running":
            self._proc.terminate()
            with self._lock:
                self.status = "killed"
```

几个关键设计：

**输出双写**——`stdout` 既进环形缓冲（内存里 O(1) 读取最新 1000 行），又落到磁盘文件（完整历史）。用户问"最近咋样"用缓冲，需要完整日志读文件。Claude Code 的 `TaskOutput` 把这一招做得更极致——直接让 bash 子进程的 fd 指向文件，完全绕过 JS 层的管道缓冲，防止大输出把进程内存打爆（源码注释里还提到过一个"768GB 磁盘被填满"的事故）。我们用 Python 的 `buffering=1`（行缓冲）做轻量版。

**线程而非 asyncio**——Python 里 `subprocess.Popen` 的 pipe 搬运用线程最简单，`daemon=True` 确保主进程退出时不会被卡住。

**状态转换原子化**——`status` 字段只在 `_lock` 里改，避免"进程已经结束了但 kill() 还在把 status 标成 killed"的竞态。

---

## 任务注册表：按 id 查、按 id kill

一个全局单例管所有后台任务：

```python
# agent/tools/background/registry.py

import threading
from .shell import BackgroundShellCommand

class TaskRegistry:
    def __init__(self):
        self._tasks: dict[str, BackgroundShellCommand] = {}
        self._lock = threading.Lock()

    def spawn(self, command: str, cwd: str | None = None) -> BackgroundShellCommand:
        task = BackgroundShellCommand(command, cwd=cwd)
        with self._lock:
            self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> BackgroundShellCommand | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_running(self) -> list[BackgroundShellCommand]:
        with self._lock:
            return [t for t in self._tasks.values() if t.status == "running"]

background_registry = TaskRegistry()
```

配套两个新工具，让 LLM 能查询和终止：

```python
# agent/tools/background/tools.py

class BashOutputTool(Tool):
    name = "bash_output"
    description = "读取后台任务的最近输出（最多 1000 行）"

    def call(self, input: dict[str, Any]) -> str:
        task = background_registry.get(input["task_id"])
        if not task:
            return f"错误：找不到任务 {input['task_id']}"
        return (
            f"task_id: {task.id}\n"
            f"status: {task.status}\n"
            f"exit_code: {task.exit_code}\n"
            f"--- 最近输出 ---\n{task.read_tail()}"
        )

class KillBashTool(Tool):
    name = "kill_bash"
    description = "终止一个后台任务"

    def call(self, input: dict[str, Any]) -> str:
        task = background_registry.get(input["task_id"])
        if not task:
            return f"错误：找不到任务 {input['task_id']}"
        task.kill()
        return f"任务 {task.id} 已终止"
```

到这里，三个工具（`bash` 带后台参数、`bash_output`、`kill_bash`）就能让 LLM 完整管理后台任务了。但还差最关键的一块——**任务结束后，怎么告诉 LLM？**

---

## 核心难点：完成后怎么通知 Agent

这是整篇教程最关键的部分。先看一个看起来很自然但**会毁掉用户体验**的朴素方案。

### 朴素方案：任务完成就触发一轮 LLM 调用

"任务结束了，我就 append 一条 user message，然后立即调用 LLM 继续对话"——这是绝大多数人的第一反应。但它在真实交互环境下会出现这几种糟糕情况：

1. 用户正在输入框里打字，`npm run dev` 挂了，程序突然以"用户"身份提交一条消息并触发 LLM 流式回复，把用户的输入框冲掉或者与 LLM 响应交错
2. 同一时间如果有 3 个后台任务同时结束，就会连续触发 3 次 LLM 调用，token 消耗爆炸
3. 用户关掉 CLI 或者 Agent 正处在 Plan Mode、权限确认对话框等状态，被硬插一条消息会让状态机乱掉

问题的根源是：**把"通知"和"触发推理"绑死了**。

### Claude Code 的方案：入队搭车

Claude Code 的做法简单但巧妙——任务完成时**只入队，不触发**。具体数据流：

```
进程结束
   ↓
BackgroundShellCommand._pump 把 status 改为 completed
   ↓
on_task_finished(task) → 格式化一段 XML → 入全局队列
   ↓
[队列里躺着，谁都不动]
   ↓
下一次 query 循环开始时（也就是用户真的按回车发新消息时）
   ↓
query 循环在"工具执行完、下一次 LLM API 调用前"drain 队列
   ↓
把队列里的内容作为 attachment 合并进当轮请求
   ↓
LLM 看到用户新消息 + 一堆 <task-notification> 标签，自行决定怎么回应
```

关键性质：

- **不主动唤起 LLM**——没有用户输入就不会凭空多一次调用
- **用户输入框不受影响**——队列和输入组件是两套独立状态
- **搭车即可**——用户下一条消息自带 LLM 调用开销，顺路把通知捎上几乎零额外成本
- **天然合并**——10 个任务同时完成，下一轮一起带过去，只发一次 API 请求

以下是 `query.ts` 里那段关键注释的原话：

> Drain pending notifications. LocalShellTask completions are 'next' (when MONITOR_TOOL is on) and drain without Sleep. Other task types (agent/workflow/framework) still default to 'later' — the Sleep flush covers those.

也就是说，这个队列是带优先级的——**用户输入永远优先**，后台通知默认是 `later`（稍后顺路带上），只有用户真正开口时它们才有机会出现在 LLM 面前。

注意括号里的限定 `when MONITOR_TOOL is on`——MONITOR_TOOL 是 Claude Code 在部分模式下给 shell 通知"升舱"到 `next` 的专用机制，我们这个教学版没有实现。所以**我们代码里所有 shell 任务的通知都走默认 `later`**，和 agent/workflow 通知同档。这也是后面 SleepTool + drain 阈值升级这条路径存在的前提——没有它，later 级通知永远没机会送达 LLM。

### 但这只解决了一半

搭车机制保证了"不打断"。但它也带来一个新问题——**如果用户就是不说话呢？**

想象这个场景：

```
用户: 跑一下 `pytest` 然后告诉我结果
Agent: 好，我扔后台了。（调 bash run_in_background）
[用户盯着屏幕不动，等结果]
[pytest 跑完，通知入队，priority=later]
[队列躺着没人来 drain]
[用户越等越困惑]
```

如果这时候 Claude Code 真的一直沉默，用户体验就废了。但实测它并不会沉默——它会主动给你回复"测试跑完了，全绿"。这说明**还有第二条路径**把后台通知送到 LLM 面前，而我上面只讲了第一条。

下一节补齐。

---

## 实现通知队列

先定义入队数据结构，几乎是对 `messageQueueManager.ts` 的最小移植：

```python
# agent/tools/background/notifications.py

import threading
from dataclasses import dataclass, field
from typing import Literal

Priority = Literal["now", "next", "later"]
_PRIORITY_ORDER = {"now": 0, "next": 1, "later": 2}

@dataclass
class QueuedNotification:
    value: str                          # XML 格式的通知内容
    mode: str = "task-notification"     # 类型标识
    priority: Priority = "later"
    agent_id: str | None = None         # 子 Agent 通知隔离用

class NotificationQueue:
    """全局通知队列——任务完成后只入队，不主动触发 LLM。"""

    def __init__(self):
        self._queue: list[QueuedNotification] = []
        self._lock = threading.Lock()

    def enqueue(self, notif: QueuedNotification) -> None:
        with self._lock:
            self._queue.append(notif)

    def drain(self, agent_id: str | None = None) -> list[QueuedNotification]:
        """取出所有属于当前 Agent 的通知，按优先级排序。
        由 Agent 主循环在"下一次 LLM 调用前"调用。"""
        with self._lock:
            mine = [n for n in self._queue if n.agent_id == agent_id]
            for n in mine:
                self._queue.remove(n)
            mine.sort(key=lambda n: _PRIORITY_ORDER[n.priority])
            return mine

notification_queue = NotificationQueue()
```

然后回填到 `BackgroundShellCommand._pump` 结束时调用的 `on_task_finished`：

```python
# agent/tools/background/shell.py

_TASK_NOTIFICATION_TEMPLATE = (
    "<task-notification>\n"
    "<task-id>{id}</task-id>\n"
    "<task-type>shell</task-type>\n"
    "<output-file>{path}</output-file>\n"
    "<status>{status}</status>\n"
    "<summary>Command \"{cmd}\" {status_text}</summary>\n"
    "</task-notification>"
)

_STATUS_TEXT = {
    "completed": "completed successfully",
    "failed": "failed with exit code",
    "killed": "was killed",
}

def on_task_finished(task: BackgroundShellCommand) -> None:
    xml = _TASK_NOTIFICATION_TEMPLATE.format(
        id=task.id,
        path=task.output_file,
        status=task.status,
        cmd=task.command[:80],
        status_text=_STATUS_TEXT.get(task.status, task.status),
    )
    notification_queue.enqueue(QueuedNotification(value=xml, priority="later"))
```

XML 标签的选择直接沿用 Claude Code 的那套（`task-notification` / `task-id` / `status` / `summary`）。结构化标签比自由文本更稳——LLM 见过无数次类似 schema，能直接识别这是一条"工具回执式"信息而不是用户说的话。

---

## 集成到 Agent 主循环

最后一步：让 `Agent.stream()` 在每轮 LLM 调用前先 drain 队列，把通知塞进消息历史。

回顾一下（在第 03 篇建立的）主循环骨架：

```python
while True:
    response = self.llm.call(self.messages)
    if not response.tool_uses:
        break
    # 执行工具...
    self.messages.append({"role": "user", "content": tool_results})
```

我们在"下次 LLM 调用之前"插入通知 drain：

```python
def _drain_notifications(self) -> None:
    """把队列里的任务完成通知合并成一条 user message，追加到历史。"""
    notifs = notification_queue.drain(agent_id=self.agent_id)
    if not notifs:
        return
    merged = "\n\n".join(n.value for n in notifs)
    # 作为 system-attachment 风格的 user 消息追加
    # 注意：这只是追加到 messages，不会立即触发 API 调用
    self.messages.append({
        "role": "user",
        "content": f"[background-task-updates]\n{merged}",
    })

def stream(self, prompt: str):
    self.messages.append({"role": "user", "content": prompt})
    while True:
        self._drain_notifications()        # ← 搭车点：每轮调用前 drain
        response = self.llm.call(self.messages)
        if not response.tool_uses:
            break
        # ... 执行工具并写入 tool_results ...
```

几个设计决策值得展开：

**为什么放在"每轮 LLM 调用前"而不是"用户输入时"**——放在调用前意味着：用户发完消息进入 Agent 循环后，每次迭代（包括工具调用后）都会检查一次队列。这样长任务在 Agent 正在调工具时完成，**下一次 LLM 调用就能立刻看到**，不用等下一条用户消息。Claude Code 的 `query.ts` 也是这个策略："after tool calls, before API call"。

**为什么合并成一条消息而不是每条一个 message**——减少消息条目数，也让模型看到"一组"更新而不是一串离散事件，便于它一次性回复。

**为什么 priority 默认 `later`**——用户主动输入的请求（隐含 `next` 优先级）永远应该排在前面。如果用户正在问 A，后台任务完成跟 A 无关，那这条通知最好跟在用户问题之后，避免干扰模型理解当前主问题。

---

## 还有一半：SleepTool 主动等待

前面所有逻辑都在回答一个问题："用户开口时，如何不打断地把后台结果带过去"。但用户不开口的时候呢？Claude Code 的答案是——**让 Agent 自己调一个工具把自己"睡"一会儿**。

### SleepTool 的设计

给 LLM 注册一个名叫 `sleep` 的工具，prompt 直接告诉它：

> Wait for a short period, returning immediately when any background task notification arrives. Use this when you have nothing else to do but are waiting for an asynchronous result.

内部实现只做两件事：每隔 `poll_interval` 秒看一眼队列、有通知就立即返回，到了 `max_seconds` 也返回。

```python
# agent/tools/background/sleep.py

SLEEP_TOOL_NAME = "sleep"

class SleepTool(Tool):
    @property
    def name(self): return SLEEP_TOOL_NAME

    def call(self, input):
        max_seconds   = float(input.get("max_seconds", 10))
        poll_interval = float(input.get("poll_interval", 1.0))

        # 快路径：入队前已有通知→立即返回
        if notification_queue.has_notifications_for():
            return "slept: 0.00s  woke_by: notification"

        start = time.monotonic()
        deadline = start + max_seconds
        woke_by = "timeout"
        while time.monotonic() < deadline:
            step = min(poll_interval, deadline - time.monotonic())
            time.sleep(step)
            if notification_queue.has_notifications_for():
                woke_by = "notification"
                break
        return f"slept: {time.monotonic() - start:.2f}s  woke_by: {woke_by}"
```

`has_notifications_for()` 是队列新增的一个方法，跟 `drain()` 的区别是：只查不取。SleepTool 不应该消费通知——它的职责仅仅是"唤醒 Agent"，真正的消费要留给下一轮主循环的 drain。

对应的是 Claude Code 源码 `services/mcp/channelNotification.ts` 的这句关键注释：

> The notification handler wraps the content in a `<channel>` tag and enqueues it. **SleepTool polls `hasCommandsInQueue()` and wakes within 1s.**

### 关键一步：drain 阈值升级

光有 SleepTool 轮询还不够——回想一下：我们之前说 drain 默认只拉 `next` 级，后台通知（`later`）会被过滤掉。如果 SleepTool 唤醒后仍然过滤 later，这次唤醒就白唤了。

**所以先给前面的 `drain()` 多加一个 `max_priority` 参数**：默认 `'next'`（搭车场景下的安全阈值），在 Agent 主动 sleep 过后把门槛放宽到 `'later'`。被过滤掉的通知仍然留在队列里等下一次机会。

> 补一句澄清免得歧义：`now=0 / next=1 / later=2`，数值越大优先级越低。drain 的 `max_priority` 是"准入上限"——默认 `next` 只放 `now/next` 进来，改成 `later` 意味着**门槛放宽**，连最低优先级的 `later` 也一起捞。本节标题"阈值升级"说的是阈值数值变大（1→2），不是优先级升级。

Claude Code 的解法直接看 `query.ts` L1570 这行：

```ts
const sleepRan = toolUseBlocks.some(b => b.name === SLEEP_TOOL_NAME)
// ...
const queuedCommandsSnapshot = getCommandsByMaxPriority(
  sleepRan ? 'later' : 'next',
).filter(cmd => { /* ... */ })
```

逻辑是：**上一轮如果调了 SleepTool，本轮 drain 就把准入门槛放宽——除了 `next`，也把 `later` 一起拉出来**。言下之意：既然 Agent 都自己开口说它在等异步结果了，那后台通知对于它来说就不再是"打断"而是"答案"，直接捞上来。

对应在我们的 `Agent` 里就是这样：

```python
# agent/agent.py

class Agent:
    def __init__(self, ...):
        ...
        self._last_turn_sleep_ran: bool = False    # 新增一个开关

    def _drain_background_notifications(self) -> None:
        max_priority = "later" if self._last_turn_sleep_ran else "next"
        self._last_turn_sleep_ran = False     # 消费即重置
        notifs = notification_queue.drain(agent_id=None, max_priority=max_priority)
        if not notifs:
            return
        merged = "\n\n".join(n.value for n in notifs)
        self.messages.append({
            "role": "user",
            "content": f"[background-task-updates]\n{merged}",
        })

    def _track_sleep_tool_invocation(self, tool_uses):
        if any(tu.get("name") == SLEEP_TOOL_NAME for tu in tool_uses):
            self._last_turn_sleep_ran = True

    def _run_tool_loop(self, prompt):
        ...
        while turn_count < self.max_turns:
            turn_count += 1
            self._drain_background_notifications()   # 搭车点（阈值交给上面决定）
            response = self._call_llm(...)
            ...
            if response.has_tool_use:
                self._execute_tools(response.tool_uses)
                self._track_sleep_tool_invocation(response.tool_uses)   # 给下一轮留标记
```

整个状态机就三步：

1. Agent 调了 `sleep` → `_track_sleep_tool_invocation` 把标记置为 `True`
2. 下一轮进入，`_drain_background_notifications` 读到标记 → 门槛放宽到 `later` → 后台通知一并拉出来
3. 消费完重置标记（只影响紧随其后的那一轮 drain，避免持续默认为 later）

### 两条路径对照表

把两个场景并在一起看：

| 情境 | 触发的 drain | 阈值 | later 通知命运 |
|---|---|---|---|
| 用户发新消息（Agent 上一轮没 sleep） | 搭车 | `next` | **被过滤**，留在队列等下一次机会 |
| Agent 上一轮调了 `sleep` | Sleep 唤醒后下一轮 | `later` | **被捞起来** → LLM 看到 → 主动播报 |
| 后台任务还在跑时用户发消息 | 搭车 | `next` | 没影响——没通知可拉 |

这也解释了 `types/textInputTypes.ts` 里那段关于 `later` 优先级的注释：

> `later` — End-of-turn drain. Wait for the current turn to finish, then process as a new query. **Wakes an in-progress SleepTool call (query.ts upgrades the drain threshold after sleep so the message is attached to the same turn).**

"upgrades the drain threshold after sleep" 的原文语义和我们的 Python 版是一一对应的。

### Agent 什么时候调 SleepTool？

写 prompt 是设计重点——完全交给 LLM 自己决定。给它两个明确信号：

1. **什么时候用**："when you have nothing else to do but are waiting for an asynchronous result"
2. **什么时候不用**："Do NOT use this as a generic delay. If you don't have a pending async result to wait on, just end your turn instead."

捕捉的意图是区分两种无事可做：

- 开完后台任务、用户没再追问 → 调 `sleep`，记得告诉我结果
- 回答完用户的问题、没有异步任务在跑 → 直接 `end_turn`，安静等用户

失败了也无所谓：即使 LLM 误调了一次 sleep，`max_seconds` 的上限保证不会挂死。

### 顺手澄清一下：为什么这不是 Heartbeat

有一个很容易弄混的反面案例："用定时器每隔 N 秒触发一轮 LLM 检查队列"——不管你叫它 Heartbeat、Watchdog 还是定时轮询，本质都是**从外部强插一次 LLM 调用**。这会出什么局面：

- 用户还在输入——定时器到点时仍然会触发，回到打断问题
- 多轮对话期间每 N 秒挥霍 token，成本很快就爆炸
- Agent 正在干活儿（比如在 Plan Mode）被硬拉出来 → 状态机出错

SleepTool 划清界限的方式很优雅：**什么时候睡，由 Agent 自己定**。新一轮触发的来源就是那个同步的 `sleep` 工具调用的返回——和其它工具完全一样，没有任何特殊调度路径。你可以把它理解成一个能被外部事件提前返回的 `time.sleep`。

---

## 示例：一次完整的后台任务生命周期

跑通后的实际对话长这样：

```
用户: 启动开发服务器然后帮我检查一下首页组件

LLM: 好的，我先启动开发服务器。
  工具调用: bash(command="npm run dev", run_in_background=True)
  工具返回: 后台任务已启动 task_id: bg-a3f2b1c8
  工具调用: read(file="src/pages/index.tsx")
  工具返回: <文件内容>
  LLM 回复: "首页组件使用了 useState 管理..."

（几十秒后，npm run dev 因为端口占用崩了，_pump 把 status 标成 failed，
  on_task_finished 入队一条 XML，队列里静静躺着）

用户: 组件里的 useEffect 有什么问题？

（主循环开始，_drain_notifications 取出那条通知并追加到 messages）

LLM: 我看到刚才启动的开发服务器崩了（bg-a3f2b1c8 failed, exit code 1），
  端口 3000 已被占用。是先解决这个问题还是继续讨论 useEffect？
```

整个过程中用户不会被抢话——失败通知是跟着用户下一条消息一起带进去的。LLM 甚至可以选择"先回答用户问的 useEffect 问题，顺便提一句后台任务挂了"。

---

## 与 Claude Code 源码实现的对比

我们这个简化版保留了四层架构骨架，省略了不少工程细节：

| | Claude Code（TS 版） | 我们的实现（Python 版） |
|---|---|---|
| **后台化触发** | 显式 `run_in_background` + 超时自动后台化 + Assistant Mode 15 秒预算 | 只做显式 `run_in_background` |
| **输出管理** | 子进程 fd 直写文件、绕过 JS 层；Size Watchdog 每 5 秒 stat 文件防磁盘爆 | Python `Popen` + 行缓冲文件 + 1000 行环形缓冲 |
| **状态机** | `running → backgrounded → completed/killed`，可从前台转后台 | `running → completed/failed/killed`，只有一条线 |
| **卡死检测** | Stall Watchdog：45 秒无新输出 + 尾部匹配 `PROMPT_PATTERNS`（`(y/n)`、`Press Enter` 等）→ 判定为等用户输入 | 没做 |
| **通知队列** | 全局 `commandQueue` + 优先级 now/next/later + agentId 隔离 | 同构的轻量版 |
| **队列消费（被动）** | `query.ts` 在"tool 执行完、API 调用前"drain；bash 模式和 slash command 被特殊排除 | 主循环每轮前 drain |
| **队列消费（主动）** | `SleepTool` 轮询 `hasCommandsInQueue()`；`sleepRan` 使下一轮 drain 阈值从 `next` 升级到 `later` | `SleepTool` + `_last_turn_sleep_ran` 开关，语义一一对应 |
| **SleepTool 可见性** | 仅 proactive/KAIROS 模式暴露 | 所有场景都注册（教学简化），prompt 里限定用途 |
| **UI 隔离** | `NON_EDITABLE_MODES` 禁止通知消息被 UP/ESC 拉入输入框 | 我们无 TUI，不涉及 |

其中最值得一学的是 Claude Code 的 **Assistant Mode 自动后台化**：即使 LLM 没指定 `run_in_background`，如果一个命令阻塞超过 `ASSISTANT_BLOCKING_BUDGET_MS`（15 秒），且当前处于 KAIROS（无人值守）模式，系统会自动把它转到后台，避免 Agent 白等。实现上只是一个 `setTimeout` + 状态检查，但对"长任务容错"有质变级别的提升。

> **教学简化**：Claude Code 的 `ShellCommand` + `TaskOutput` + `LocalShellTask` 加起来 3000 多行 TypeScript，光 `TaskOutput` 的文件/管道模式切换就有一整套状态机。我们这里只保留了最核心的"后台进程 + 输出双写 + 通知搭车"三件事，满足教学目的。

---

## 为什么这套设计能跑得稳

抽离出来六条可复用的设计模式：

**1. 通知和推理解耦**——入队不等于触发。这是整个机制能"不打断用户"的根因。`enqueue` 只动数据，对通知的消费由 Agent 主循环决定时机。

**2. 搭车优于外插 Heartbeat**——用户开口时复用当前这一次 LLM 调用，比外部定时器强插更自然、更省 token，也不会踩到输入、权限确认等状态机。

**3. SleepTool 把控制权还给 Agent**——用户不说话时，是否要主动等异步结果、等多久，全由 LLM 自己调度。新一轮的触发客体仍然是工具调用返回这条常规通路，没有旁路。

**4. 优先级表达"谁更急"**——`now/next/later` 三档就够了。用户输入隐含 `next`，系统通知 `later`，中断信号 `now`。drain 阈值从 `next` 升级到 `later` 就能优雅地表达"我现在真的在等后台结果"这件事。

**5. 双写输出应对大量数据**——内存环形缓冲给"最近咂样"用，磁盘文件给"完整日志"用。两者的读写路径独立，互不影响。

**6. 状态机原子化**——`status` 字段只在锁内转换，避免 `pump` 线程写完 `completed`、`kill()` 又写成 `killed` 这种竞态。

---

## 小结

| 设计决策 | 选择 | 原因 |
|---|---|---|
| 后台化触发 | LLM 显式指定 `run_in_background` | 简单直接；自动后台化留作进阶 |
| 输出存储 | 环形缓冲（内存）+ 追加文件（磁盘） | 兼顾快速查尾和完整历史 |
| 通知机制（开口时） | 全局队列 + 主循环搭车 drain（阈值 `next`） | 不打断用户、天然合并、零额外 API 调用 |
| 通知机制（用户沉默时） | `SleepTool` 轮询 + 下一轮 drain 阈值升级到 `later` | 不需要外部定时器，控制权在 LLM |
| 通知格式 | `<task-notification>` XML | 结构化、模型易识别 |
| 优先级默认 | `later` | 用户永远优先 |
| drain 集成点 | 每轮 LLM 调用前 | 工具调用后新完成的任务也能即时被看到 |

```
LLM: bash(npm run dev, run_in_background=True)
         ↓
    spawn BackgroundShellCommand
         ↓
    返回 task_id，Agent 循环继续
         ↓
    [进程在后台跑着，主对话正常进行]
         ↓
    进程结束 → enqueue(<task-notification>, priority=later)
         ↓
    [队列躺着，不触发任何事]
         ↓
    【路径 A】用户下一条消息 或 Agent 下一轮工具循环
         → drain 阈值 next → later 被过滤，留在队列
    【路径 B】Agent 主动调 sleep(max_seconds=N)
         → SleepTool 轮询队列 → 有通知 → 下一轮
         → drain 阈值 升级为 later → 通知进入 messages
         → LLM 看到通知，自然地提及或处理
```

把四层（工具、进程、输出、通知）解耦，每一层独立可替换——比如把通知队列换成 Redis List 就支持多进程 Agent 共享，把 `BackgroundShellCommand` 换成 K8s Job 就支持远程执行。这种"积木式"设计在 Agent 工程里非常值钱。
