"""
Auto Compact —— 自动压缩历史

对应 reference 中的 services/compact/autoCompact.ts

核心功能：
  - 估算消息历史的 token 数
  - 接近上限时自动压缩
  - 用 LLM 生成摘要替换历史

压缩策略：
  1. 保留 system prompt（如果有）
  2. 保留最近 N 轮完整对话
  3. 对更早的历史生成摘要
  4. 用摘要替换原始消息

注意事项：
  - 压缩会丢失细节，只保留高层语义
  - 压缩本身需要调用 LLM，有成本和延迟
  - 需要权衡压缩频率和上下文质量
"""

from dataclasses import dataclass
from typing import Any

from agent.llm.base import BaseLLM


# 默认触发压缩的阈值（token 数）
DEFAULT_COMPACT_THRESHOLD = 80000

# 默认压缩后保留的最近轮数
DEFAULT_KEEP_RECENT_ROUNDS = 4

# 压缩提示模板
COMPACT_PROMPT = """请对以下对话历史进行摘要。保留关键信息和决策，去除细节。

对话历史：
{history}

请生成简洁的摘要，包含：
1. 讨论的主要话题
2. 做出的关键决策
3. 当前状态

摘要："""


@dataclass
class CompactResult:
    """压缩结果"""
    summary: str           # 生成的摘要
    original_tokens: int   # 原始 token 数
    new_tokens: int        # 压缩后 token 数
    removed_messages: int  # 移除的消息数
    kept_messages: int     # 保留的消息数


def estimate_tokens(messages: list[dict]) -> int:
    """
    估算消息列表的 token 数。

    简单估算：每 3 个字符约 1 个 token。
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "")
                    if isinstance(text, str):
                        total_chars += len(text)
    return total_chars // 3 + 1


def should_compact(
    messages: list[dict],
    threshold: int = DEFAULT_COMPACT_THRESHOLD,
) -> bool:
    """
    检查是否需要压缩。

    Args:
        messages: 消息历史
        threshold: 压缩阈值

    Returns:
        是否需要压缩
    """
    tokens = estimate_tokens(messages)
    return tokens >= threshold


def compact_messages(
    messages: list[dict],
    llm: BaseLLM,
    keep_recent: int = DEFAULT_KEEP_RECENT_ROUNDS,
) -> CompactResult:
    """
    压缩消息历史。

    策略：
      1. 分离 system prompt（如果有）
      2. 保留最近 keep_recent 轮完整对话
      3. 对更早的历史生成摘要
      4. 合并为新的消息列表

    Args:
        messages: 原始消息历史
        llm: LLM 实例（用于生成摘要）
        keep_recent: 保留的最近轮数

    Returns:
        CompactResult 包含摘要和统计信息
    """
    original_tokens = estimate_tokens(messages)

    # 分离 system prompt（第一个 role=system 的消息）
    system_msg = None
    other_messages = []
    for msg in messages:
        if msg.get("role") == "system" and system_msg is None:
            system_msg = msg
        else:
            other_messages.append(msg)

    # 计算一轮 = user + assistant（可能还有 tool）
    # 简单处理：每两个消息算一轮（user + assistant）
    total_pairs = len(other_messages) // 2
    keep_count = min(keep_recent * 2, len(other_messages))

    # 保留最近的消息
    recent_messages = other_messages[-keep_count:] if keep_count > 0 else []

    # 需要压缩的早期消息
    old_messages = other_messages[:-keep_count] if keep_count < len(other_messages) else []

    if not old_messages:
        # 没有需要压缩的消息
        return CompactResult(
            summary="",
            original_tokens=original_tokens,
            new_tokens=original_tokens,
            removed_messages=0,
            kept_messages=len(messages),
        )

    # 生成摘要
    history_text = "\n\n".join([
        f"{msg.get('role', 'unknown')}: {msg.get('content', '')[:500]}"
        for msg in old_messages
    ])

    compact_prompt = COMPACT_PROMPT.format(history=history_text)

    # 调用 LLM 生成摘要
    response = llm.chat(
        messages=[{"role": "user", "content": compact_prompt}],
        system="你是一个对话摘要助手。请生成简洁的摘要。",
    )

    summary = response.text or "[对话历史摘要]"

    # 构建新的消息列表
    new_messages = []
    if system_msg:
        new_messages.append(system_msg)

    # 添加摘要作为 system 消息
    new_messages.append({
        "role": "system",
        "content": f"[历史摘要] {summary}",
    })

    # 添加保留的最近消息
    new_messages.extend(recent_messages)

    new_tokens = estimate_tokens(new_messages)

    return CompactResult(
        summary=summary,
        original_tokens=original_tokens,
        new_tokens=new_tokens,
        removed_messages=len(old_messages),
        kept_messages=len(recent_messages) + (1 if system_msg else 0),
    )


def maybe_compact(
    messages: list[dict],
    llm: BaseLLM,
    threshold: int = DEFAULT_COMPACT_THRESHOLD,
    keep_recent: int = DEFAULT_KEEP_RECENT_ROUNDS,
) -> tuple[list[dict], CompactResult | None]:
    """
    如果需要，执行压缩。

    Args:
        messages: 消息历史
        llm: LLM 实例
        threshold: 压缩阈值
        keep_recent: 保留的最近轮数

    Returns:
        (新消息列表, CompactResult | None)
        如果不需要压缩，返回原始列表和 None
    """
    if not should_compact(messages, threshold):
        return messages, None

    result = compact_messages(messages, llm, keep_recent)

    # 构建新消息列表
    system_msg = None
    other_messages = []
    for msg in messages:
        if msg.get("role") == "system" and system_msg is None:
            system_msg = msg
        else:
            other_messages.append(msg)

    keep_count = min(keep_recent * 2, len(other_messages))
    recent_messages = other_messages[-keep_count:] if keep_count > 0 else []

    new_messages = []
    if system_msg:
        new_messages.append(system_msg)

    new_messages.append({
        "role": "system",
        "content": f"[历史摘要] {result.summary}",
    })
    new_messages.extend(recent_messages)

    return new_messages, result
