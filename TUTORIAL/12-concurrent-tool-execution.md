# 从零实现 Coding Agent（十二）：并发工具执行

在前面的实现中，当 LLM 一次返回多个工具调用时（比如同时读取 3 个文件），Agent 是一个接一个串行执行的。如果每个 `read` 要 50ms，三个就是 150ms；而实际上它们之间毫无依赖，完全可以同时执行，总耗时只有 50ms。

这就是并发工具执行要解决的问题：**识别哪些工具可以安全地同时运行，然后并发执行它们。**

---

## 核心问题：不是所有工具都能并发

并发的前提是"无副作用冲突"。考虑这两种场景：

**可以并发：**
```
LLM: 请同时读取 a.py、b.py、c.py
→ read(a.py) + read(b.py) + read(c.py)  ← 三个只读操作，互不影响
```

**不能并发：**
```
LLM: 先写入 config.json，然后读取 config.json
→ write(config.json) + read(config.json)  ← read 依赖 write 的结果
```

如果第二种场景也并发执行，`read` 可能读到 `write` 之前的旧内容，或者读到写了一半的文件。

这意味着我们需要一个机制让每个工具声明"我这次调用能不能和别人同时跑"。

---

## 设计：is_concurrency_safe(input)

在 `Tool` 基类上添加一个方法：

```python
# agent/tools/base.py

class Tool(ABC):
    # ... name, description, input_schema, call ...

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        """
        判断本次调用是否可以与其他工具并发执行。
        默认返回 False（保守策略：假设不安全）。
        """
        return False
```

关键设计点：**接收 `input` 参数**。

同一个工具，不同的调用参数可能有不同的安全性。最典型的例子是 `bash`：

```python
bash("ls -la")       → 只读，可以并发
bash("rm -rf /tmp")  → 有副作用，不能并发
```

如果 `is_concurrency_safe` 不看 input，bash 就只能一刀切返回 `False`，白白浪费了只读命令并发的机会。

---

## 各工具的安全性分类

### 始终安全：只读工具

```python
# agent/tools/read.py
class ReadTool(Tool):
    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True  # 只读操作

# agent/tools/glob.py
class GlobTool(Tool):
    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True  # 只读操作

# agent/tools/grep.py
class GrepTool(Tool):
    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True  # 只读操作

# agent/tools/get_current_time.py
class GetCurrentTimeTool(Tool):
    def is_concurrency_safe(self, input: dict) -> bool:
        return True  # 纯函数，无副作用
```

这些工具无论怎么调用都不会修改外部状态，可以随意并发。

### 始终不安全：写操作工具

```python
# write 和 edit 使用基类默认的 False，不需要额外实现
class WriteTool(Tool):
    # is_concurrency_safe 继承基类，返回 False

class EditTool(Tool):
    # is_concurrency_safe 继承基类，返回 False
```

为什么不需要显式实现？因为基类默认返回 `False`。这是**保守默认值**设计——新工具如果忘记考虑并发安全性，默认就是串行执行，不会引入并发 bug。

### 条件判断：bash

```python
# agent/tools/bash.py
class BashTool(Tool):

    _READONLY_COMMANDS = frozenset([
        "ls", "cat", "head", "tail", "wc", "find", "which", "whoami",
        "pwd", "echo", "date", "env", "printenv", "uname", "hostname",
        "file", "stat", "du", "df", "free", "uptime", "id",
        "grep", "egrep", "fgrep", "rg", "ag", "ack",
        "diff", "cmp", "md5sum", "sha256sum", "shasum",
        "tree", "realpath", "dirname", "basename",
        "git status", "git log", "git diff", "git show", "git branch",
        "git remote", "git tag", "git rev-parse", "git ls-files",
    ])

    def _is_readonly(self, command: str) -> bool:
        cmd = command.strip()
        if not cmd:
            return False

        # 取管道/分号/&&之前的第一段命令
        for sep in ("|", "&&", "||", ";"):
            cmd = cmd.split(sep)[0].strip()

        # 检查 git 子命令（如 "git status"）
        for readonly_cmd in self._READONLY_COMMANDS:
            if " " in readonly_cmd and cmd.startswith(readonly_cmd):
                return True

        # 提取第一个 token 作为命令名
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return False
        if not tokens:
            return False

        cmd_name = os.path.basename(tokens[0])
        return cmd_name in self._READONLY_COMMANDS

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        command = input.get("command", "")
        return self._is_readonly(command)
```

`_is_readonly` 的策略是**白名单**：只有明确知道是只读的命令才返回 `True`。对于管道命令（`grep foo | wc -l`），只检查第一段——因为管道读端不修改文件系统。对于未知命令，保守返回 `False`。

这个白名单不需要完美。漏掉一些只读命令（如 `jq`、`awk`）的后果只是串行执行，不会出错。而错误地将写命令标记为只读才是真正的风险，所以白名单比黑名单更安全。

---

## 分区：把工具调用分成批次

知道每个工具是否安全后，下一步是把它们分成批次。

```python
def _partition_tool_calls(self, tool_uses: list[dict]) -> list[list[int]]:
    """
    将工具调用分区为可并发执行的批次。
    
    规则：
      - 连续的并发安全工具归入同一批次（并发执行）
      - 非并发安全工具独占一个批次（串行执行）
    """
    batches: list[list[int]] = []
    current_batch: list[int] = []
    current_is_safe = None

    for i, tool_use in enumerate(tool_uses):
        is_safe = self._is_tool_concurrency_safe(tool_use)

        if current_is_safe is None:
            current_batch = [i]
            current_is_safe = is_safe
        elif is_safe and current_is_safe:
            current_batch.append(i)
        else:
            batches.append(current_batch)
            current_batch = [i]
            current_is_safe = is_safe

    if current_batch:
        batches.append(current_batch)

    return batches
```

看几个例子：

```
输入: [read, glob, grep, write, read, read]
安全: [True, True, True, False, True, True]
分区: [[0,1,2], [3], [4,5]]
      ^^^^^^^^  ^^^  ^^^^^^^
      并发3个   独占  并发2个

输入: [write, edit]
安全: [False, False]
分区: [[0], [1]]
      ^^^^  ^^^^
      独占  独占

输入: [read, read, read]
安全: [True, True, True]
分区: [[0,1,2]]
      ^^^^^^^^
      并发3个
```

非安全工具充当**执行屏障**（barrier）：前面的批次必须全部完成，才能执行非安全工具，然后才能继续后面的批次。

---

## 并发执行：ThreadPoolExecutor

Python 的 `concurrent.futures.ThreadPoolExecutor` 是最直接的并发工具。虽然有 GIL 限制，但我们的工具主要做 I/O（文件读取、进程调用），ThreadPoolExecutor 完全够用。

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def _execute_tools(self, tool_uses: list[dict]) -> list[str]:
    results = [None] * len(tool_uses)
    batches = self._partition_tool_calls(tool_uses)

    for batch in batches:
        if len(batch) == 1:
            # 单个工具，直接执行（避免线程池开销）
            idx = batch[0]
            results[idx] = self._execute_tool(tool_uses[idx])
        else:
            # 多个并发安全工具，使用线程池
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                future_to_idx = {
                    executor.submit(self._execute_tool, tool_uses[idx]): idx
                    for idx in batch
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        results[idx] = future.result()
                    except Exception as e:
                        results[idx] = f"错误：工具并发执行失败 - {e}"

    return results
```

几个关键决策：

1. **单个工具不创建线程池**：`if len(batch) == 1` 直接执行。线程池有创建和调度开销，对于单个工具是浪费。
2. **`as_completed` 而非 `map`**：`as_completed` 在任何 future 完成时立即返回，我们可以尽早处理结果。虽然最终都要等全部完成，但错误可以更早发现。
3. **结果按索引存储**：`results[idx]` 而非 `results.append()`，确保结果顺序与 tool_uses 一致，即使并发完成顺序不同。
4. **异常兜底**：`except Exception` 确保单个工具崩溃不会影响整个批次。

---

## 流式模式的特殊处理

在 `stream()` 方法中，我们还需要 yield 事件给调用方。并发模式下的事件顺序变了：

**串行模式（旧）：**
```
tool_use_start(read a.py)
tool_use_end(result of a.py)
tool_use_start(read b.py)
tool_use_end(result of b.py)
```

**并发模式（新）：**
```
tool_use_start(read a.py)    ← 批次内所有开始事件先发
tool_use_start(read b.py)
tool_use_end(result of a.py) ← 并发执行后，按原始顺序发结束事件
tool_use_end(result of b.py)
```

```python
def stream(self, prompt: str) -> Iterator[StreamEvent]:
    # ... 省略前面的代码 ...

    # 执行所有工具调用（支持并发）
    batches = self._partition_tool_calls(response.tool_uses)
    results = [None] * len(response.tool_uses)

    for batch in batches:
        # 先 yield 当前批次所有工具的开始事件
        for idx in batch:
            tool_use = response.tool_uses[idx]
            yield StreamEvent(
                type="tool_use_start",
                name=tool_use.get("name", ""),
                input=tool_use.get("input"),
            )

        if len(batch) == 1:
            idx = batch[0]
            results[idx] = self._execute_tool(response.tool_uses[idx])
            yield StreamEvent(type="tool_use_end", result=results[idx])
        else:
            # 并发执行...（同 _execute_tools）
            # 按原始顺序 yield 结束事件
            for idx in batch:
                yield StreamEvent(type="tool_use_end", result=results[idx])
```

为什么先发所有 `start` 再发 `end`？因为这更真实地反映了并发执行的时序——所有工具同时开始，然后陆续完成。如果交错发 `start`/`end`，调用方（比如终端 UI）会以为工具是串行执行的。

---

## 与 Claude Code 源码实现的对比

Claude Code（TypeScript 版）有两套并发系统，而我们只实现了一套简化版：

| | Claude Code（TS 版） | 我们的实现（Python 版） |
|---|---|---|
| **执行模型** | StreamingToolExecutor（流式队列） + toolOrchestration（批次） | 纯批次模型 |
| **并发机制** | Promise.race + async generator | ThreadPoolExecutor |
| **流式执行** | 工具边流入边执行（不等 API 响应结束） | 收集完所有 tool_use 后再执行 |
| **错误传播** | Bash 错误取消兄弟工具 | 单个工具错误不影响批次内其他工具 |
| **结果顺序** | 保证按到达顺序 yield | 保证按原始索引顺序 |
| **最大并发数** | 可配置（默认 10，环境变量控制） | 等于批次大小 |

Claude Code 的 `StreamingToolExecutor` 更复杂——它支持在 API 响应还没结束时就开始执行已经收到的工具调用。这需要一个队列和状态机来管理。我们的实现更简单：等 API 返回所有 tool_use 后，再分区并发执行。对于大多数场景，这个延迟差异可以忽略。

Claude Code 还有一个"Bash 错误级联"机制：如果一个 `bash` 工具失败，会取消所有兄弟工具。这是因为 shell 命令经常有隐式依赖链（`mkdir` 失败后续命令就没意义了）。我们暂时没有实现这个——保持简单。

---

## 一个微妙的边界：为什么不对 write 和 edit 做依赖分析？

你可能会想：`write(a.py)` 和 `write(b.py)` 写不同的文件，应该可以并发啊？

理论上可以，但我们选择不做。原因：

1. **依赖不总是显式的**：`write(config.py)` 可能被后续的 `bash(python config.py)` 读取。跨工具的依赖无法从单个工具的 input 中看出。
2. **LLM 的意图无法确定**：LLM 返回 `[write(a.py), write(b.py)]` 时，我们不知道它是否期望按顺序执行。
3. **收益有限**：写操作通常很快（毫秒级），并发节省的时间微乎其微。

保守策略（写操作串行）的成本极低，但风险为零。这是正确的工程取舍。

---

## 小结

并发工具执行的本质是：**分类 → 分区 → 调度**。

```
LLM 返回多个 tool_use
      ↓
每个工具声明 is_concurrency_safe(input)
      ↓
相邻的安全工具分为一组
非安全工具独占一组
      ↓
每组依次执行：
  - 安全组 → ThreadPoolExecutor 并发
  - 非安全组 → 直接串行执行
      ↓
结果按原始顺序返回
```

| 设计决策 | 选择 | 原因 |
|---|---|---|
| 默认安全性 | `False` | 保守优先，新工具不会意外并发 |
| 安全性粒度 | 每次调用（看 input） | bash 的只读/写入命令不同 |
| 并发机制 | ThreadPoolExecutor | I/O 密集型任务，GIL 不是瓶颈 |
| 错误策略 | 单工具错误不影响同批次其他工具 | 保持简单，工具间通常无依赖 |
| 结果顺序 | 按原始 tool_use 索引 | 保证 tool_result 消息顺序一致 |

下一篇将实现**子 Agent** —— 主 Agent 启动独立子 Agent 执行子任务，这是从"单一循环"到"多 Agent 协作"的关键一步。
