"""
Tool Result Budget —— 大输出截断

对应 reference 中的 utils/toolResultStorage.ts

核心功能：
  - 检测 tool result 大小
  - 超过阈值时自动截断
  - 防止撑爆 context window

设计原则：
  - 优先保留开头和结尾（通常包含关键信息）
  - 中间部分用省略号表示
  - 告知 LLM 内容已被截断
"""

import re
from typing import Any


# 默认阈值：单个 tool result 最大字符数
DEFAULT_MAX_TOOL_RESULT_LENGTH = 10000

# 截断后保留的开头字符数
DEFAULT_HEAD_LENGTH = 3000

# 截断后保留的结尾字符数
DEFAULT_TAIL_LENGTH = 3000


def truncate_tool_result(
    content: str,
    max_length: int = DEFAULT_MAX_TOOL_RESULT_LENGTH,
    head_length: int = DEFAULT_HEAD_LENGTH,
    tail_length: int = DEFAULT_TAIL_LENGTH,
) -> str:
    """
    截断 tool result，防止超过上下文窗口。

    策略：
      1. 如果内容长度 <= max_length，直接返回
      2. 如果内容长度 > max_length：
         - 保留开头 head_length 字符
         - 保留结尾 tail_length 字符
         - 中间用 "... [内容已截断，共 X 字符] ..." 替代

    Args:
        content: 原始内容
        max_length: 最大允许长度
        head_length: 保留的开头长度
        tail_length: 保留的结尾长度

    Returns:
        截断后的内容
    """
    if len(content) <= max_length:
        return content

    if head_length + tail_length >= max_length:
        # 如果头尾长度之和超过最大长度，简单截断
        return content[:max_length] + f"\n... [内容已截断，共 {len(content)} 字符]"

    head = content[:head_length]
    tail = content[-tail_length:]
    omitted = len(content) - head_length - tail_length

    return (
        f"{head}\n"
        f"... [内容已截断，省略 {omitted} 字符，共 {len(content)} 字符] ...\n"
        f"{tail}"
    )


def count_tokens_approx(text: str) -> int:
    """
    估算文本的 token 数（粗略估计）。

    简单规则：
      - 英文单词：约 0.75 tokens/词
      - 中文字符：约 1 token/字符
      - 标点符号：约 0.5 tokens/个

    这是一个非常粗略的估计，实际 token 数取决于具体模型和 tokenizer。

    Args:
        text: 输入文本

    Returns:
        估算的 token 数
    """
    if not text:
        return 0

    # 简单估算：平均每个 token 约 4 个字符（英文）或 1 个字符（中文）
    # 这里使用一个保守的估计：每 3 个字符算 1 个 token
    return len(text) // 3 + 1


def check_context_budget(
    messages: list[dict],
    max_tokens: int = 100000,
    warning_threshold: float = 0.8,
) -> dict[str, Any]:
    """
    检查上下文预算使用情况。

    Args:
        messages: 消息历史
        max_tokens: 最大 token 数
        warning_threshold: 警告阈值（0-1）

    Returns:
        {
            "total_tokens": 估算的总 token 数,
            "usage_ratio": 使用比例,
            "is_warning": 是否超过警告阈值,
            "is_exceeded": 是否超过最大限制,
        }
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # content blocks
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "")
                    if isinstance(text, str):
                        total_chars += len(text)

    # total_chars 已经是字符数，直接计算 token 数
    total_tokens = total_chars // 3 + 1 if total_chars > 0 else 0
    usage_ratio = total_tokens / max_tokens if max_tokens > 0 else 0

    return {
        "total_tokens": total_tokens,
        "usage_ratio": usage_ratio,
        "is_warning": usage_ratio >= warning_threshold,
        "is_exceeded": usage_ratio >= 1.0,
    }
