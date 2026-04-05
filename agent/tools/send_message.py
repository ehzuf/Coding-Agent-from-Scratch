"""
SendMessage 工具

对应 Claude Code 源码中的 SendMessage 机制

核心思想：
  主 Agent 可以向已存在的子 Agent 发送后续消息，
  继续一个已经建立的对话上下文。

用途：
  1. 任务追问：子 Agent 完成初步分析后，主 Agent 追加更具体的问题
  2. 结果细化：基于子 Agent 返回的结果，要求进一步处理
  3. 多轮子任务：将一个复杂任务分阶段交给同一个子 Agent

设计要点：
  1. 通过 agentId 定位子 Agent（从 agent 工具返回结果中获取）
  2. 子 Agent 保留完整对话历史，理解后续问题的上下文
  3. 不可并发安全（同一个子 Agent 不应同时收到两条消息）
"""

import time
from typing import Any


class SendMessageTool:
    """
    向已存在的子 Agent 发送消息，继续对话。

    通过 agentId 找到之前创建的子 Agent，
    发送新消息并返回结构化结果。

    注意：和 AgentTool 一样，这不是在 __init__.py 中注册的常规工具。
    它由 Agent 类在初始化时自动创建和注入，
    因为它需要访问子 Agent 注册表。
    """

    def __init__(self, agent_registry: dict):
        """
        初始化 SendMessage 工具。

        Args:
            agent_registry: 子 Agent 注册表（与 AgentTool 共享）
        """
        self._agent_registry = agent_registry

    @property
    def name(self) -> str:
        return "send_message"

    @property
    def description(self) -> str:
        return """向已存在的子 Agent 发送后续消息，继续对话。

使用场景：
- 子 Agent 完成初步任务后，需要追问或细化结果
- 基于子 Agent 返回的分析，要求进一步处理
- 需要在已有的对话上下文中继续工作（避免重复建立上下文）

使用前提：
- 必须提供有效的 agentId（从之前 agent 工具的返回结果中获取）
- 子 Agent 保留了完整的对话历史，可以理解后续问题的上下文

注意：如果需要全新的独立任务，应该使用 agent 工具而非 send_message。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "目标子 Agent 的 ID（从 agent 工具返回的 agentId 字段获取）",
                },
                "message": {
                    "type": "string",
                    "description": "要发送给子 Agent 的消息内容",
                },
            },
            "required": ["agent_id", "message"],
        }

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        # 同一个子 Agent 不应并发接收消息（消息历史会冲突）
        # 保守起见返回 False
        return False

    def to_api_format(self) -> dict[str, Any]:
        """转换为 API 工具定义格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def call(self, input: dict[str, Any]) -> str:
        """
        向已存在的子 Agent 发送消息，继续对话。

        查找注册表中的子 Agent，发送新消息，
        返回结构化结果。
        """
        agent_id = input.get("agent_id", "")
        message = input.get("message", "")

        if not agent_id:
            return "错误：agent_id 参数不能为空"
        if not message:
            return "错误：message 参数不能为空"

        # 查找子 Agent
        sub_agent = self._agent_registry.get(agent_id)
        if sub_agent is None:
            available = list(self._agent_registry.keys())
            if available:
                return (
                    f"错误：找不到 agentId='{agent_id}' 的子 Agent。"
                    f"可用的 agentId: {available}"
                )
            else:
                return "错误：当前没有任何子 Agent。请先使用 agent 工具创建子 Agent。"

        start_time = time.monotonic()

        # 继续对话（子 Agent 保留了之前的消息历史）
        response = sub_agent.chat(message)

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # 构建结构化结果
        content = response.text or "（子 Agent 未返回文本结果）"

        result_parts = [
            f"[SendMessage → {agent_id}]",
            f"status: completed",
            "",
            content,
            "",
            f"--- 统计: "
            f"turns={sub_agent.turn_count}, "
            f"tool_uses={sub_agent._tool_use_count}, "
            f"duration={duration_ms}ms, "
            f"total_input_tokens={sub_agent._total_input_tokens}, "
            f"total_output_tokens={sub_agent._total_output_tokens} ---",
        ]

        return "\n".join(result_parts)
