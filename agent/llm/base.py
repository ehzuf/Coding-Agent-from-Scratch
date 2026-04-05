"""
LLM 抽象基类

定义所有 LLM 后端必须实现的统一接口。
上层代码只依赖这个接口，不感知具体是 Anthropic 还是 OpenAI。

消息格式统一使用 OpenAI 风格（因为它已成为事实标准）：
  {"role": "user" | "assistant" | "system", "content": "..."}

Tool Use 支持：
  - chat() 接受 tools 参数
  - LLMResponse.content 是 content blocks 列表，每个 block 可能是：
    - {"type": "text", "text": "..."}
    - {"type": "tool_use", "id": "...", "name": "...", "input": {...}}

Prompt Caching 支持：
  - LLMResponse 新增 cache_read_tokens / cache_write_tokens 字段
  - 记录本次请求命中缓存读取的 token 数 和 写入缓存的 token 数
  - 这两个字段来自 Anthropic 的 usage.cache_read_input_tokens / usage.cache_creation_input_tokens
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class LLMResponse:
    """
    一次完整的 LLM 响应。

    content 是 content blocks 列表，每个 block 可能是：
      - {"type": "text", "text": "回复文本"}
      - {"type": "tool_use", "id": "xxx", "name": "工具名", "input": {...}}

    提供辅助属性：
      - .text: 提取所有文本块的拼接结果
      - .tool_uses: 提取所有 tool_use 块
      - .has_tool_use: 是否包含工具调用
    """

    content: list[dict[str, Any]]  # content blocks 列表
    input_tokens: int              # 消耗的输入 token 数（不含缓存命中）
    output_tokens: int             # 消耗的输出 token 数
    model: str                     # 实际使用的模型名称
    stop_reason: str | None = None # 停止原因：end_turn / tool_use / max_tokens
    # Prompt Caching
    cache_read_tokens: int = 0     # 本次从缓存读取的 token 数（已命中，不计费或折扣计费）
    cache_write_tokens: int = 0    # 本次写入缓存的 token 数（首次建立缓存）

    @property
    def text(self) -> str:
        """提取所有文本块的拼接结果。"""
        texts = []
        for block in self.content:
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "".join(texts)

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        """提取所有 tool_use 块。"""
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def has_tool_use(self) -> bool:
        """是否包含工具调用。"""
        return len(self.tool_uses) > 0


class BaseLLM(ABC):
    """
    LLM 后端抽象基类。

    每个具体后端（Anthropic、OpenAI 等）继承此类并实现两个方法：
    - chat()      : 非流式，等待完整回复后返回
    - stream()    : 流式，逐块 yield 文本片段（暂不支持 tool_use）
    """

    def __init__(self, model: str, api_key: str):
        self.model = model
        self.api_key = api_key

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 8096,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """
        非流式调用。等待 LLM 生成完整回复后返回。

        Args:
            messages:   消息历史，格式为 [{"role": "...", "content": "..."}]
            system:     系统提示（可选）
            max_tokens: 最大输出 token 数
            tools:      可用工具列表，格式为 [{"name": "...", "description": "...", "input_schema": {...}}]

        Returns:
            LLMResponse 包含 content blocks 和 token 用量
        """
        ...

    @abstractmethod
    def stream(
        self,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 8096,
        tools: list[dict] | None = None,
    ) -> Iterator[str]:
        """
        流式调用。逐块 yield 文本片段，调用方实时打印。

        注意：流式模式下暂不支持 tool_use，工具调用请使用 chat()。

        Args:
            messages:   消息历史
            system:     系统提示（可选）
            max_tokens: 最大输出 token 数
            tools:      可用工具列表（流式模式下通常不使用）

        Yields:
            str: 文本片段（可能是单个字符，也可能是几个词）
        """
        ...
