# 从零实现 Coding Agent（一）：项目骨架与 LLM 抽象层

这是系列文章的第一篇，我们将从零开始，一步步实现一个精简版的 coding agent。整个系列以 Claude Code 的 TypeScript 实现为参考，用 Python 重构核心功能，深入理解 coding agent 的设计原理。

## 为什么从 LLM 抽象层开始？

很多人上手 coding agent 时，会直接把 API 调用散落在各处代码里。这样做的问题很快就会暴露：

1. **换个模型就要改 N 处代码**
2. **不同 API 的参数格式各异**，Anthropic 的 system prompt 是独立参数，OpenAI 要塞进 messages 数组
3. **测试困难**，无法轻松 mock LLM 响应

解决之道是引入**抽象层**——定义统一接口，让上层代码不感知底层是 Claude 还是 GPT。

## 设计抽象基类

我们的目标是定义一个 `BaseLLM` 类，让所有后端都实现相同的接口：

```python
# agent/llm/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

@dataclass
class LLMResponse:
    """统一封装 LLM 响应"""
    content: str              # 回复文本
    input_tokens: int         # 输入 token 数
    output_tokens: int        # 输出 token 数
    stop_reason: str | None   # 停止原因


class BaseLLM(ABC):
    """LLM 抽象基类，定义统一接口"""

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """非流式调用，返回完整响应"""
        pass

    @abstractmethod
    def stream(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: list[dict] | None = None,
    ) -> Iterator[str]:
        """流式调用，逐块返回文本"""
        pass
```

看起来很简单？但魔鬼在细节里。让我们看看两个主流 API 的差异。

## Anthropic SDK 实现

Anthropic 的 API 设计比较"正统"，system prompt 是独立参数，流式输出有专门的 `text_stream`：

```python
# agent/llm/anthropic_llm.py

from anthropic import Anthropic
from .base import BaseLLM, LLMResponse

class AnthropicLLM(BaseLLM):
    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        self.client = Anthropic()
        self.model = model

    def chat(self, messages, *, system=None, tools=None) -> LLMResponse:
        kwargs = {"messages": messages, "model": self.model, "max_tokens": 4096}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        # 可选：禁用 Claude 的 extended thinking
        kwargs["thinking"] = {"type": "disabled"}

        response = self.client.messages.create(**kwargs)

        # 过滤 ThinkingBlock，只保留文本
        text_blocks = [b for b in response.content if b.type == "text"]
        text = "".join(b.text for b in text_blocks)

        return LLMResponse(
            content=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
        )

    def stream(self, messages, *, system=None, tools=None) -> Iterator[str]:
        kwargs = {"messages": messages, "model": self.model, "max_tokens": 4096}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        kwargs["thinking"] = {"type": "disabled"}

        with self.client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text
```

有几个值得注意的点：

- **ThinkingBlock 过滤**：Claude 某些模型会返回"思考过程"块，我们只关心最终文本
- **`stream.text_stream`**：Anthropic SDK 已经帮我们过滤好了，直接迭代就是纯文本

## OpenAI SDK 实现

OpenAI 的 API 设计有些"特立独行"，system prompt 要作为第一条 message 插入：

```python
# agent/llm/openai_llm.py

from openai import OpenAI
from .base import BaseLLM, LLMResponse

class OpenAILLM(BaseLLM):
    def __init__(self, model: str = "gpt-4o", base_url: str | None = None):
        self.client = OpenAI(base_url=base_url)
        self.model = model

    def chat(self, messages, *, system=None, tools=None) -> LLMResponse:
        # OpenAI 要求 system 作为第一条 message
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        kwargs = {"messages": full_messages, "model": self.model}
        if tools:
            kwargs["tools"] = tools

        response = self.client.chat.completions.create(**kwargs)

        return LLMResponse(
            content=response.choices[0].message.content or "",
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            stop_reason=response.choices[0].finish_reason,
        )

    def stream(self, messages, *, system=None, tools=None) -> Iterator[str]:
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        kwargs = {"messages": full_messages, "model": self.model, "stream": True}
        if tools:
            kwargs["tools"] = tools

        for chunk in self.client.chat.completions.create(**kwargs):
            delta = chunk.choices[0].delta
            if delta.content is not None:
                yield delta.content
```

关键差异对比：

| 差异点 | Anthropic | OpenAI |
|--------|-----------|--------|
| system prompt | 独立参数 `system=` | 插入 messages 数组首位 |
| token 字段名 | `input_tokens` / `output_tokens` | `prompt_tokens` / `completion_tokens` |
| 流式获取文本 | `stream.text_stream` 自动过滤 | 手动检查 `delta.content is not None` |

这些差异被封装在各自的实现里，上层调用者完全不需要关心。

## 工厂函数：让选择后端变得简单

现在我们有两个实现，如何方便地切换？用工厂函数：

```python
# agent/llm/__init__.py

from .anthropic_llm import AnthropicLLM
from .openai_llm import OpenAILLM
from .base import BaseLLM, LLMResponse

def create_llm(
    provider: str,
    model: str | None = None,
    base_url: str | None = None,
) -> BaseLLM:
    """根据 provider 创建对应的 LLM 实例"""
    if provider == "anthropic":
        return AnthropicLLM(model=model or "claude-sonnet-4-20250514")
    elif provider == "openai":
        return OpenAILLM(model=model or "gpt-4o", base_url=base_url)
    else:
        raise ValueError(f"Unknown provider: {provider}")
```

使用时：

```python
from agent.llm import create_llm

# 用 Anthropic
llm = create_llm("anthropic")

# 用 OpenAI（或兼容服务）
llm = create_llm("openai", base_url="https://api.deepseek.com/v1")

# 之后完全一致
response = llm.chat([{"role": "user", "content": "你好"}])
```

## CLI 入口：第一次对话

有了 LLM 层，我们可以写一个最简单的命令行入口：

```python
# agent/__main__.py

import argparse
from agent.llm import create_llm

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", help="用户问题")
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    llm = create_llm(args.provider, args.model)
    messages = [{"role": "user", "content": args.prompt}]
    response = llm.chat(messages)
    print(response.content)

if __name__ == "__main__":
    main()
```

运行测试：

```bash
python -m agent "用三句话介绍 Python"
```

## 这一步我们学到了什么

1. **抽象层的重要性**：统一接口让切换后端变得无痛
2. **API 差异封装**：system prompt 处理、token 字段映射等细节不应暴露给上层
3. **工厂模式**：用字符串标识创建具体实例，配置与代码解耦

这只是一个开始。下一步，我们将实现**流式输出**——让回复像打字机一样逐字显示，这是提升用户体验的关键。
