"""
Anthropic 后端实现

将统一的 BaseLLM 接口适配到 Anthropic SDK。

关键差异点（相比 OpenAI 风格）：
1. system prompt 是独立参数，不在 messages 列表里
2. 流式响应使用 client.messages.stream() 上下文管理器
3. token 用量在 response.usage 中，字段名略有不同
4. 部分模型（如思考模型）会在 content 列表里混入 ThinkingBlock，
   需要过滤只取 TextBlock 和 ToolUseBlock

Prompt Caching：
- 在 system prompt 的最后一个 block 添加 cache_control，让 Anthropic 缓存 system prefix
- 在消息历史较早位置的消息上打 cache_control 标记，缓存长消息历史
- cache_control 要求消息内容 content 是 list[block] 格式，不能是纯字符串
- Anthropic 最多支持 4 个 cache breakpoints
"""

import os
from dataclasses import dataclass
from typing import Any, Iterator

import anthropic

from .base import BaseLLM, LLMResponse


@dataclass
class StreamEvent:
    """
    流式事件，支持文本和 tool_use。

    类型：
      - "text": 文本片段，text 字段包含内容
      - "tool_use_start": 开始 tool_use block，name/id/input 字段
      - "tool_use_delta": tool_use 参数增量，input_delta 字段
      - "tool_use_end": tool_use block 完成
      - "message_end": 消息结束，包含 usage 信息
    """
    type: str
    text: str = ""
    name: str = ""
    id: str = ""
    input: dict | None = None
    input_delta: str = ""
    usage: dict | None = None
    stop_reason: str | None = None

# 显式禁用 extended thinking。
# 某些兼容服务默认开启 thinking，不禁用会导致响应极慢且 content[0] 不是 TextBlock。
_THINKING_DISABLED = {"type": "disabled"}

# cache_control 标记，应用到 content block 上
_CACHE_CONTROL = {"type": "ephemeral"}


def _add_cache_control_to_system(system: str) -> list[dict]:
    """
    将 system prompt 字符串转换为带 cache_control 的 content blocks 格式。

    Anthropic 要求：cache_control 必须加在 content block 上（不能是纯字符串），
    且打在最后一个 block 上，表示"缓存到此为止的所有内容"。

    输入：  "你是一个助手..."
    输出：  [{"type": "text", "text": "你是一个助手...", "cache_control": {"type": "ephemeral"}}]
    """
    return [
        {
            "type": "text",
            "text": system,
            "cache_control": _CACHE_CONTROL,
        }
    ]


def _add_cache_control_to_messages(messages: list[dict]) -> list[dict]:
    """
    在消息历史中添加 cache_control 标记。

    策略（与 Claude Code 一致）：
    - 在最后一条消息上打 cache_control 标记（无论角色是什么）
      Claude Code 直接在 messages[messages.length - 1] 上打标记，
      不搜索特定角色，确保尽可能多的内容被缓存
    - 每次调用只打 1 个 breakpoint（加上 system 共最多 2 个，留余量给 tools）
    - 注意：这里是浅拷贝，只替换需要修改的消息，不修改原始 messages

    Returns:
        修改后的 messages 列表（原始列表不被修改）
    """
    if not messages:
        return messages

    cache_idx = len(messages) - 1

    result = list(messages)  # 浅拷贝列表
    msg = result[cache_idx]
    content = msg.get("content")

    if isinstance(content, str):
        # 字符串 content 需要转换为 block 格式才能添加 cache_control
        new_content = [{"type": "text", "text": content, "cache_control": _CACHE_CONTROL}]
        result[cache_idx] = {**msg, "content": new_content}
    elif isinstance(content, list) and content:
        # 已经是 block 格式，给最后一个 block 加 cache_control
        new_blocks = list(content)
        last_block = dict(new_blocks[-1])
        last_block["cache_control"] = _CACHE_CONTROL
        new_blocks[-1] = last_block
        result[cache_idx] = {**msg, "content": new_blocks}

    return result


class AnthropicLLM(BaseLLM):
    """Anthropic Claude 后端"""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        enable_cache: bool = True,
    ):
        """
        Args:
            model:        模型名称
            api_key:      API 密钥（可选，默认从 ANTHROPIC_API_KEY 读取）
            enable_cache: 是否启用 prompt caching（默认 True）
        """
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        super().__init__(model=model, api_key=resolved_key)
        self._client = anthropic.Anthropic(api_key=resolved_key)
        self.enable_cache = enable_cache

    def _build_kwargs(
        self,
        messages: list[dict],
        system: str | None,
        max_tokens: int,
        tools: list[dict] | None,
    ) -> dict:
        # 如果启用缓存，对消息历史添加 cache_control 标记
        if self.enable_cache:
            messages = _add_cache_control_to_messages(messages)

        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            messages=messages,
            thinking=_THINKING_DISABLED,
        )
        if system:
            # 如果启用缓存，将 system prompt 转为带 cache_control 的 block 格式
            if self.enable_cache:
                kwargs["system"] = _add_cache_control_to_system(system)
            else:
                kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        return kwargs

    def _content_to_blocks(self, content: list[Any]) -> list[dict[str, Any]]:
        """
        将 Anthropic SDK 的 content 列表转换为统一的 content blocks 格式。

        只保留 text 和 tool_use 类型，过滤掉 thinking 等其他类型。
        """
        blocks = []
        for block in content:
            if block.type == "text":
                blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
            # 其他类型（thinking 等）过滤掉
        return blocks

    def chat(
        self,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 8096,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        response = self._client.messages.create(
            **self._build_kwargs(messages, system, max_tokens, tools)
        )

        # 读取 prompt cache 用量
        # cache_creation_input_tokens: 首次建立缓存时写入的 token 数
        # cache_read_input_tokens: 命中缓存读取的 token 数（不计费或折扣计费）
        usage = response.usage
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

        return LLMResponse(
            content=self._content_to_blocks(response.content),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
            stop_reason=response.stop_reason,
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
        )

    def stream(
        self,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 8096,
        tools: list[dict] | None = None,
    ) -> Iterator[str]:
        # Anthropic 流式 API 使用上下文管理器
        # text_stream 只 yield 文本片段，自动跳过 ThinkingBlock 等非文本事件
        # 注意：流式模式下不支持 tool_use，工具调用请使用 chat()
        with self._client.messages.stream(
            **self._build_kwargs(messages, system, max_tokens, tools)
        ) as s:
            yield from s.text_stream

    def stream_with_events(
        self,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 8096,
        tools: list[dict] | None = None,
    ) -> Iterator[StreamEvent]:
        """
        流式调用，返回完整事件流，支持 tool_use。

        与 stream() 不同，这个方法：
        1. 使用原始流事件（不是 text_stream）
        2. 支持 tool_use block 的流式接收
        3. 返回 StreamEvent 对象，调用方需要处理不同类型的事件

        事件序列示例（纯文本）：
          StreamEvent(type="text", text="Hello")
          StreamEvent(type="text", text=" world")
          StreamEvent(type="message_end", usage={...}, stop_reason="end_turn")

        事件序列示例（带 tool_use）：
          StreamEvent(type="text", text="Let me check")
          StreamEvent(type="tool_use_start", name="get_current_time", id="tool_123", input={})
          StreamEvent(type="tool_use_delta", input_delta='{"timezone": "UTC"}')
          StreamEvent(type="tool_use_end")
          StreamEvent(type="message_end", usage={...}, stop_reason="tool_use")
        """
        kwargs = self._build_kwargs(messages, system, max_tokens, tools)

        # 使用原始流 API（不是 messages.stream() 上下文管理器）
        # 这样可以获取所有原始事件
        stream = self._client.messages.create(**kwargs, stream=True)

        # 当前正在构建的 tool_use block
        current_tool_use = None
        accumulated_input = ""

        for event in stream:
            event_type = event.type

            if event_type == "content_block_start":
                block = event.content_block
                if block.type == "tool_use":
                    # 开始新的 tool_use block
                    current_tool_use = {
                        "id": block.id,
                        "name": block.name,
                        "input": block.input if hasattr(block, 'input') else {},
                    }
                    accumulated_input = ""
                    yield StreamEvent(
                        type="tool_use_start",
                        id=block.id,
                        name=block.name,
                        input=current_tool_use["input"],
                    )
                elif block.type == "text":
                    # 文本 block 开始，通常没有内容
                    pass

            elif event_type == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    # 文本增量
                    yield StreamEvent(type="text", text=delta.text)
                elif delta.type == "input_json_delta":
                    # tool_use 参数增量
                    accumulated_input += delta.partial_json
                    yield StreamEvent(
                        type="tool_use_delta",
                        input_delta=delta.partial_json,
                    )

            elif event_type == "content_block_stop":
                if current_tool_use is not None:
                    # tool_use block 完成
                    # 尝试解析完整的 input JSON
                    try:
                        import json
                        if accumulated_input:
                            current_tool_use["input"] = json.loads(accumulated_input)
                    except json.JSONDecodeError:
                        # JSON 不完整，使用已解析的部分
                        pass

                    yield StreamEvent(
                        type="tool_use_end",
                        id=current_tool_use["id"],
                        name=current_tool_use["name"],
                        input=current_tool_use["input"],
                    )
                    current_tool_use = None
                    accumulated_input = ""

            elif event_type == "message_delta":
                # 消息结束，包含 usage 和 stop_reason
                usage = None
                if hasattr(event, 'usage') and event.usage:
                    usage = {
                        "input_tokens": getattr(event.usage, 'input_tokens', 0),
                        "output_tokens": getattr(event.usage, 'output_tokens', 0),
                        "cache_read_tokens": getattr(event.usage, 'cache_read_input_tokens', 0),
                        "cache_write_tokens": getattr(event.usage, 'cache_creation_input_tokens', 0),
                    }

                stop_reason = None
                if hasattr(event.delta, 'stop_reason'):
                    stop_reason = event.delta.stop_reason

                yield StreamEvent(
                    type="message_end",
                    usage=usage,
                    stop_reason=stop_reason,
                )

            elif event_type == "message_start":
                # 消息开始，可以在这里获取初始 usage
                pass
