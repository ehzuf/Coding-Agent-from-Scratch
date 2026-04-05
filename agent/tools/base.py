"""
Tool 抽象基类

对应 reference 中的 Tool.ts，精简版只保留最核心的四个要素：
  - name: 工具名称（LLM 调用时使用）
  - description: 工具描述（LLM 决定是否调用时参考）
  - input_schema: 输入参数的 JSON Schema
  - call(): 执行工具，返回结果字符串

Tool Use 协议的本质：
  1. Agent 把所有可用工具的 name + description + input_schema 发给 LLM
  2. LLM 决定需要调用某个工具时，返回一个 tool_use block：
     {"type": "tool_use", "id": "...", "name": "工具名", "input": {...参数...}}
  3. Agent 执行工具，得到结果字符串
  4. Agent 把结果封装为 tool_result block，发给 LLM：
     {"type": "tool_result", "tool_use_id": "...", "content": "结果字符串"}
  5. LLM 基于工具结果继续生成回复（可能再次调用工具，或直接回答）

这是一个循环：LLM 请求 → 执行工具 → 回传结果 → LLM 继续
直到 LLM 不再请求工具（stop_reason === "end_turn"）。
"""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """
    工具抽象基类。

    所有工具必须实现：
      - name: str          — 工具名称
      - description: str   — 工具描述（给 LLM 看）
      - input_schema: dict — JSON Schema 格式的参数定义
      - call(input) -> str — 执行工具，返回结果
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，LLM 调用时使用。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，LLM 决定是否调用时参考。"""
        ...

    @property
    @abstractmethod
    def input_schema(self) -> dict[str, Any]:
        """
        输入参数的 JSON Schema。

        示例：
        {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "时区，如 Asia/Shanghai",
                }
            },
            "required": []
        }
        """
        ...

    @abstractmethod
    def call(self, input: dict[str, Any]) -> str:
        """
        执行工具。

        Args:
            input: 从 LLM 的 tool_use block 中解析出的参数

        Returns:
            工具执行结果字符串。出错时返回错误信息（不抛异常），
            让 LLM 自行决定如何处理错误。
        """
        ...

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        """
        判断本次调用是否可以与其他工具并发执行。

        并发安全意味着该操作没有副作用，或者不依赖于其他工具的执行顺序。
        典型的并发安全操作：读取文件、搜索内容、获取时间等。
        典型的非并发安全操作：写入文件、编辑文件、执行写命令等。

        子类可以根据 input 参数动态判断（如 bash 工具对只读命令返回 True）。
        默认返回 False（保守策略：假设不安全）。

        Args:
            input: 工具调用的参数

        Returns:
            True 表示可以并发执行，False 表示需要独占执行
        """
        return False

    def to_api_format(self) -> dict[str, Any]:
        """
        转换为 Anthropic API 需要的工具定义格式。

        Returns:
            {"name": "...", "description": "...", "input_schema": {...}}
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
