"""
OpenAI 后端实现

将统一的 BaseLLM 接口适配到 OpenAI SDK。

同时兼容所有 OpenAI 兼容的 API（如 DeepSeek、本地 Ollama 等），
只需传入不同的 base_url 即可。

关键差异点（相比 Anthropic）：
1. system prompt 作为 role="system" 的消息放入 messages 列表首位
2. 流式使用 stream=True 参数，响应是 delta 增量对象
3. token 用量在 response.usage 中，字段名为 prompt_tokens / completion_tokens
4. Tool Use 格式不同：
   - 工具定义：{"type": "function", "function": {"name": ..., "parameters": ...}}
   - 请求工具：message.tool_calls[].function.{name, arguments}
   - 回传结果：{"role": "tool", "tool_call_id": ..., "content": ...}

Prompt Caching：
- OpenAI gpt-4o / gpt-4o-mini 等模型支持自动 prompt caching，无需额外标记
- 命中缓存的 token 数可从 usage.prompt_tokens_details.cached_tokens 读取
- DeepSeek 等兼容服务也支持类似机制，字段相同
"""

import json
import os
from typing import Any, Iterator

from openai import OpenAI

from .base import BaseLLM, LLMResponse
from .anthropic_llm import StreamEvent


def _get_cached_tokens(usage) -> int:
    """
    从 OpenAI usage 对象中提取缓存命中的 token 数。

    OpenAI 的 prompt caching 是自动进行的（无需手动标记），
    命中缓存的 token 数在 usage.prompt_tokens_details.cached_tokens 里。
    不支持 caching 的模型或服务该字段可能为 None。
    """
    try:
        details = usage.prompt_tokens_details
        if details is None:
            return 0
        cached = getattr(details, "cached_tokens", 0)
        return cached or 0
    except Exception:
        return 0


def _tools_to_openai_format(tools: list[dict]) -> list[dict]:
    """
    将统一的工具定义格式转换为 OpenAI 格式。

    输入（Anthropic 风格）：
        {"name": "...", "description": "...", "input_schema": {...}}

    输出（OpenAI 风格）：
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object"}),
            }
        }
        for t in tools
    ]


def _parse_tool_calls(message) -> list[dict[str, Any]]:
    """
    解析 OpenAI 的 tool_calls，转换为统一的 content blocks 格式。

    OpenAI 格式：
        message.tool_calls = [
            {id: "call_xxx", type: "function", function: {name: "...", arguments: "{...}"}}
        ]

    统一格式：
        [{"type": "tool_use", "id": "..., "name": ..., "input": {...}}]
    """
    if not message.tool_calls:
        return []

    tool_uses = []
    for tc in message.tool_calls:
        try:
            # arguments 是 JSON 字符串，需要解析
            input_args = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            input_args = {}

        tool_uses.append({
            "type": "tool_use",
            "id": tc.id,
            "name": tc.function.name,
            "input": input_args,
        })

    return tool_uses


class OpenAILLM(BaseLLM):
    """
    OpenAI 后端，兼容所有 OpenAI 协议的服务。

    示例：
        # 标准 OpenAI
        llm = OpenAILLM(model="gpt-4o")

        # DeepSeek（OpenAI 兼容）
        llm = OpenAILLM(
            model="deepseek-chat",
            api_key="sk-...",
            base_url="https://api.deepseek.com",
        )

        # 本地 Ollama（OpenAI 兼容）
        llm = OpenAILLM(
            model="llama3",
            api_key="ollama",
            base_url="http://localhost:11434/v1",
        )

    extra_body 参数说明：
        某些兼容服务默认开启 extended thinking，
        会通过 extra_body 传入服务商专有字段来禁用，例如：
        extra_body={"enable_thinking": False}
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        extra_body: dict | None = None,
    ):
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        # base_url 优先级：显式参数 > OPENAI_BASE_URL 环境变量 > SDK 默认值
        # OpenAI SDK 本身也会读 OPENAI_BASE_URL，这里显式处理让逻辑更透明
        resolved_url = base_url or os.environ.get("OPENAI_BASE_URL")
        super().__init__(model=model, api_key=resolved_key)
        self._client = OpenAI(api_key=resolved_key, base_url=resolved_url)
        # extra_body 会透传给每次 API 请求，用于服务商专有扩展参数
        self._extra_body = extra_body or {}

    def _build_messages(self, messages: list[dict], system: str | None) -> list[dict]:
        """
        将 system prompt 插入到 messages 列表首位。

        OpenAI 的 system 消息就是普通消息，role 为 "system"，
        这与 Anthropic 将 system 作为独立参数的方式不同。
        """
        if system:
            return [{"role": "system", "content": system}] + messages
        return messages

    def chat(
        self,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 8096,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        full_messages = self._build_messages(messages, system)

        # 转换工具定义为 OpenAI 格式
        openai_tools = _tools_to_openai_format(tools) if tools else None

        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=full_messages,
            tools=openai_tools,
            extra_body=self._extra_body or None,
        )

        message = response.choices[0].message

        # 构建统一的 content blocks
        content: list[dict[str, Any]] = []

        # 添加文本内容（如果有）
        if message.content:
            content.append({"type": "text", "text": message.content})

        # 添加工具调用（如果有）
        tool_uses = _parse_tool_calls(message)
        content.extend(tool_uses)

        # stop_reason 映射
        finish_reason = response.choices[0].finish_reason
        stop_reason = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
        }.get(finish_reason, finish_reason)

        return LLMResponse(
            content=content,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            model=response.model,
            stop_reason=stop_reason,
            # 读取 prompt cache 命中数
            # OpenAI 自动 caching，无需手动标记，只需读取用量
            # usage.prompt_tokens_details 可能为 None（不支持 caching 的模型/服务）
            cache_read_tokens=_get_cached_tokens(response.usage),
        )

    def stream(
        self,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 8096,
        tools: list[dict] | None = None,
    ) -> Iterator[str]:
        full_messages = self._build_messages(messages, system)

        # OpenAI 流式：stream=True 返回 chunk 迭代器
        # 每个 chunk.choices[0].delta.content 是文本片段（可能为 None）
        # 注意：流式模式下不支持 tool_use
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=full_messages,
            stream=True,
            extra_body=self._extra_body or None,
        )

        for chunk in response:
            delta = chunk.choices[0].delta
            if delta.content is not None:
                yield delta.content

    def stream_with_events(
        self,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 8096,
        tools: list[dict] | None = None,
    ) -> Iterator[StreamEvent]:
        """
        流式调用，返回完整事件流，支持 tool_use。

        OpenAI 的流式 tool_use 与 Anthropic 不同：
        - tool_calls 在单个 chunk 中完整返回（不是增量）
        - 需要累积所有 chunk 中的 tool_calls
        """
        full_messages = self._build_messages(messages, system)
        openai_tools = _tools_to_openai_format(tools) if tools else None

        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=full_messages,
            tools=openai_tools,
            stream=True,
            extra_body=self._extra_body or None,
            stream_options={"include_usage": True},  # 请求返回 usage 信息
        )

        accumulated_text = ""
        current_tool_calls = []  # OpenAI 的 tool_calls 在流中累积
        usage_info = {}
        finish_reason = None

        for chunk in response:
            # 某些 chunk 可能没有 choices（如 usage-only chunk）
            if not chunk.choices:
                # 获取 usage（某些服务在单独的 chunk 中返回）
                if hasattr(chunk, 'usage') and chunk.usage:
                    usage_info = {
                        "input_tokens": getattr(chunk.usage, 'prompt_tokens', 0),
                        "output_tokens": getattr(chunk.usage, 'completion_tokens', 0),
                        "cache_read_tokens": _get_cached_tokens(chunk.usage),
                    }
                continue

            delta = chunk.choices[0].delta

            # 处理文本增量
            if delta.content is not None:
                accumulated_text += delta.content
                yield StreamEvent(type="text", text=delta.content)

            # 处理 tool_calls（OpenAI 的 tool_calls 在流中逐步累积）
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    # 找到或创建对应的 tool_call
                    existing = None
                    for existing_tc in current_tool_calls:
                        if existing_tc.get("index") == tc.index:
                            existing = existing_tc
                            break

                    if existing is None:
                        # 新的 tool_call
                        new_tc = {
                            "index": tc.index,
                            "id": tc.id or "",
                            "name": tc.function.name or "",
                            "arguments": tc.function.arguments or "",
                            "start_event_sent": False,  # 标记是否已发送 start 事件
                        }
                        current_tool_calls.append(new_tc)
                        existing = new_tc

                    # 累积参数
                    if tc.function.arguments:
                        existing["arguments"] += tc.function.arguments

                    # 更新名称（如果之前为空）
                    if tc.function.name and not existing["name"]:
                        existing["name"] = tc.function.name

                    # 更新 id（如果之前为空）
                    if tc.id and not existing["id"]:
                        existing["id"] = tc.id

                    # 发送 start 事件（只发送一次，当有名稱且未发送过）
                    if existing["name"] and not existing["start_event_sent"]:
                        yield StreamEvent(
                            type="tool_use_start",
                            id=existing["id"],
                            name=existing["name"],
                            input={},
                        )
                        existing["start_event_sent"] = True

            # 检查 finish_reason
            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

            # 获取 usage（通常在最后一个 chunk）
            if hasattr(chunk, 'usage') and chunk.usage:
                usage_info = {
                    "input_tokens": getattr(chunk.usage, 'prompt_tokens', 0),
                    "output_tokens": getattr(chunk.usage, 'completion_tokens', 0),
                    "cache_read_tokens": _get_cached_tokens(chunk.usage),
                }

        # 处理完成的 tool_calls，发送 end 事件
        for tc in current_tool_calls:
            try:
                input_args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                input_args = {}

            yield StreamEvent(
                type="tool_use_end",
                id=tc["id"],
                name=tc["name"],
                input=input_args,
            )

        # 发送 message_end 事件
        stop_reason = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
        }.get(finish_reason, finish_reason)

        yield StreamEvent(
            type="message_end",
            usage=usage_info,
            stop_reason=stop_reason,
        )


def build_tool_result_message(tool_use_id: str, content: str) -> dict:
    """
    构建 OpenAI 格式的 tool_result 消息。

    OpenAI 格式：
        {"role": "tool", "tool_call_id": "...", "content": "..."}

    对应 Anthropic 格式：
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]}
    """
    return {
        "role": "tool",
        "tool_call_id": tool_use_id,
        "content": content,
    }
