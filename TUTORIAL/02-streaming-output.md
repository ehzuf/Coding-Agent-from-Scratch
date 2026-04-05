# 从零实现 Coding Agent（二）：流式输出

在上一篇文章中，我们实现了 LLM 抽象层，完成了单轮对话。但有一个问题：调用 `llm.chat()` 后要等待好几秒，用户盯着空白终端不知道发生了什么。

这就是流式输出要解决的问题——让回复像打字机一样逐字显示，用户立刻看到"有东西在出来"。

## 为什么流式输出很重要？

从用户体验角度看，流式输出有三个关键好处：

1. **即时反馈**：用户按下回车后立即看到响应开始，而不是干等
2. **降低焦虑**：等待时间长时，看到文字逐字出现比盯着空白屏幕安心得多
3. **打断机会**：如果回复开始跑偏，用户可以提前打断，不用等完整输出

从技术角度看，流式输出是**增量处理**思想的一个应用。后续我们会看到，coding agent 的很多能力都建立在这个思想上。

## 实现流式输出

上一篇文章已经在 `BaseLLM` 定义了 `stream()` 方法：

```python
@abstractmethod
def stream(self, messages, *, system=None) -> Iterator[str]:
    """流式调用，逐块返回文本"""
    pass
```

Anthropic 和 OpenAI 的实现也已经写好。现在的问题是：如何在 CLI 中使用？

### 流式 CLI

修改 `__main__.py`：

```python
def run_once(llm, prompt: str, use_stream: bool):
    messages = [{"role": "user", "content": prompt}]

    if use_stream:
        # 流式模式
        for chunk in llm.stream(messages):
            print(chunk, end="", flush=True)
        print()  # 最后换行
    else:
        # 非流式模式
        response = llm.chat(messages)
        print(response.content)
        print(f"\n--- token 用量: 输入 {response.input_tokens}, 输出 {response.output_tokens} ---")
```

关键的细节是 `flush=True`。默认情况下，`print()` 会缓冲输出，可能不会立即显示。加上 `flush=True` 强制每次都写入终端，保证打字机效果。

### 为什么非流式还有价值？

你可能会问：既然流式体验更好，为什么保留非流式模式？

答案是**调试和测试**。非流式返回完整的 `LLMResponse` 对象，包含 token 用量、停止原因等信息。这些在调试时非常有用，但流式模式下很难准确统计（尤其是 token 用量需要等流结束后才能获取）。

## 流式输出的技术细节

让我们深入看看两个后端的流式实现差异。

### Anthropic 的流式 API

Anthropic SDK 提供了一个优雅的上下文管理器：

```python
with client.messages.stream(**kwargs) as stream:
    for text in stream.text_stream:
        yield text
```

`text_stream` 属性自动过滤掉非文本块（如 tool_use、thinking），只返回纯文本。这是 Anthropic API 设计贴心的地方。

### OpenAI 的流式 API

OpenAI 的流式 API 需要更多手动处理：

```python
for chunk in client.chat.completions.create(stream=True, **kwargs):
    delta = chunk.choices[0].delta
    if delta.content is not None:
        yield delta.content
```

这里有几个坑：

1. **delta 可能为空**：某些 chunk 只包含元数据，`delta.content` 是 `None`
2. **要手动检查**：必须加 `if delta.content is not None` 判断
3. **chunks 结构复杂**：每层都要小心访问，可能遇到 `choices` 为空列表的情况

### token 统计的差异

流式模式下，token 统计的方式不同：

| | Anthropic | OpenAI |
|---|---|---|
| 获取时机 | 流结束后通过 `stream.get_final_message()` | 最后一个 chunk 包含 `usage` 字段 |
| 统计准确性 | 精确 | 某些实现可能是估算 |

在实际项目中，如果需要精确统计 token，建议在流结束后从 API 响应中获取，而不是自己估算。

## 运行测试

```bash
# 流式（默认）
python -m agent "用三句话介绍 Python"

# 非流式
python -m agent --no-stream "用三句话介绍 Python"
```

流式模式下，你会看到文字逐字出现；非流式模式下，会等待一会儿后一次性显示完整回复和 token 统计。

## 这一步我们学到了什么

1. **用户体验优先**：流式输出显著改善等待体验，是 LLM 应用的标配
2. **`flush=True` 很关键**：Python 的输出缓冲机制可能破坏打字机效果
3. **API 差异要封装**：Anthropic 和 OpenAI 的流式 API 设计差异很大，统一接口让上层代码更简洁
4. **非流式仍有价值**：调试和测试场景需要完整响应信息

下一篇文章，我们将实现**多轮对话**——让 agent 记住上下文，这是对话系统的基础能力。
