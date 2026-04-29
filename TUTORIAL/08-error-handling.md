# 从零实现 Coding Agent（八）：Max Turns 保护 + 错误处理

前面的文章实现了 coding agent 的核心功能。现在，让我们让它更健壮——处理各种错误情况，避免陷入无限循环。

## 为什么需要错误处理？

真实世界的代码会面临各种问题：

1. **网络不稳定**：API 超时、连接断开
2. **工具执行失败**：文件不存在、权限不足、命令错误
3. **无限循环**：LLM 反复调用工具，无法收敛到答案
4. **资源耗尽**：内存不足、磁盘满了

没有错误处理的 agent 会在遇到这些问题时崩溃。我们需要：
- 优雅地处理错误
- 在可能的情况下自动恢复
- 给用户清晰的反馈

## Max Turns 保护

### 问题：无限循环

考虑这个场景：

```
用户: "帮我找 bug"
LLM: [read] 读取文件 A
     [read] 读取文件 B
     [grep] 搜索某个模式
     [read] 读取文件 C
     [bash] 运行测试
     测试失败
     [read] 再看文件 A
     [grep] 再搜索
     ...（无限循环）
```

LLM 可能陷入"再试一次"的循环，永远无法给出最终答案。

### 解决方案：限制 Turn 数

我们在 Agent 初始化时就设置了 `max_turns`：

```python
class Agent:
    def __init__(self, ..., max_turns: int = 20):
        self.max_turns = max_turns
```

在 Tool Use 循环中检查：

```python
def _run_tool_loop(self, prompt: str) -> LLMResponse:
    turn_count = 0
    while turn_count < self.max_turns:
        turn_count += 1
        # ... 执行 turn ...

    # 达到限制
    return LLMResponse(
        content=[{"type": "text", "text": f"[错误] 达到最大 turn 数限制 ({self.max_turns})"}],
        stop_reason="max_turns",
    )
```

### 为什么是 20？

- **太小**：复杂任务可能需要多轮工具调用
- **太大**：无限循环会浪费大量时间和 token
- **20 是经验值**：大多数任务在 10 轮内完成，给 2 倍余量

## 指数退避重试

### 问题：瞬时网络错误

API 调用可能因网络抖动失败，但立即重试可能成功。

### 解决方案：指数退避

```python
# agent/retry.py

import time
import random
from functools import wraps


class RetryError(Exception):
    """重试耗尽后抛出的异常。"""


def with_retry(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt >= max_retries:
                        raise RetryError(f"{max_retries} 次重试后仍然失败") from e

                    # 计算下次延迟（带抖动）
                    jitter = random.uniform(0, 0.1 * delay)
                    sleep_time = min(delay + jitter, max_delay)
                    time.sleep(sleep_time)
                    delay *= backoff_factor

        return wrapper
    return decorator
```

### 退避策略

| 尝试 | 延迟 | 说明 |
|------|------|------|
| 1 | 1.0s | 初始延迟 |
| 2 | 2.0s | 翻倍 |
| 3 | 4.0s | 再翻倍 |
| 4 | 8.0s | ... |

**为什么需要抖动？**

如果多个客户端同时遇到错误并同时重试，会造成服务器压力突增（thundering herd）。随机抖动分散重试时间。

### 集成到 Agent

```python
class Agent:
    def _call_llm_with_retry(self, api_tools: list[dict] | None) -> LLMResponse:
        if self.enable_retry:
            @with_retry(max_retries=self.max_retries)
            def _call():
                return self.llm.chat(self.messages, system=self.system, tools=api_tools)
            return _call()
        else:
            return self.llm.chat(self.messages, system=self.system, tools=api_tools)
```

## 工具错误优雅处理

### 问题：工具抛异常

如果工具执行时抛出异常，整个 agent 会崩溃。

### 解决方案：捕获所有异常

```python
def safe_tool_call(
    tool_call: Callable[..., str],
    *args,
    default_error_message: str = "工具执行失败",
    **kwargs
) -> str:
    try:
        return tool_call(*args, **kwargs)
    except Exception as e:
        return f"{default_error_message}: {type(e).__name__}: {e}"
```

### 为什么返回字符串而非抛异常？

因为 tool result 会回传给 LLM。告诉 LLM"工具失败了"，让 LLM 决定：
- 重试（可能是临时错误）
- 换其他方法
- 向用户报告错误

这比直接崩溃优雅得多。

### 集成到 Agent

```python
class Agent:
    def _execute_tool(self, tool_use: dict) -> str:
        tool = find_tool(tool_name, self.tools)

        # 使用 safe_tool_call 包装工具执行
        result = safe_tool_call(
            tool.call,
            tool_input,
            default_error_message=f"工具 '{tool_name}' 执行失败",
        )

        # 应用 budget 控制
        if self.enable_budget:
            result = truncate_tool_result(result, self.max_tool_result_length)

        return result
```

## 使用示例

### 配置 Max Turns

```python
from agent.agent import Agent

agent = Agent(
    llm=llm,
    tools=tools,
    max_turns=10,  # 更严格的限制
)

response = agent.chat("执行可能循环的任务")
if response.stop_reason == "max_turns":
    print("任务过于复杂，可能需要分解")
```

### 配置重试

```python
agent = Agent(
    llm=llm,
    tools=tools,
    enable_retry=True,
    max_retries=5,  # 更多重试
)

# 网络不稳定时更有韧性
response = agent.chat("调用外部 API")
```

### 装饰任意函数

```python
from agent.retry import with_retry

@with_retry(max_retries=3, initial_delay=2.0)
def unstable_operation():
    # 可能失败的操作
    pass
```

## 错误处理的设计哲学

1. **错误是信息**：不要隐藏错误，让 LLM 知道发生了什么
2. **区分可恢复和不可恢复**：网络错误可重试，逻辑错误不可
3. **给用户控制权**：通过参数配置重试次数、超时时间
4. **优雅降级**：即使出错，也尽量给出有用的反馈

## 这一步我们学到了什么

1. **Max Turns 防止无限循环**：设置上限，避免资源浪费
2. **指数退避处理瞬时错误**：自动重试，提高成功率
3. **工具错误优雅处理**：捕获异常，让 LLM 决定下一步
4. **错误处理是产品化必需**：原型可以不管，生产环境必须健壮

下一篇文章，我们将实现 **配置系统 + CLI 完善**——让用户可以通过配置文件和环境变量自定义 agent 行为。
