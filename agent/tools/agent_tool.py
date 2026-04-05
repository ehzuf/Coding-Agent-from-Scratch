"""
子 Agent 工具 + Agent 间通信

对应 Claude Code 源码中的 tools/AgentTool/

核心思想：
  主 Agent 可以启动一个独立的子 Agent 来执行子任务。
  子 Agent 拥有独立的消息历史和 tool use 循环，
  完成任务后将结构化结果返回给主 Agent。

功能：
  1. 结构化输出：status, agentId, content, usage 统计
  2. Agent 注册表：子 Agent 按 ID 存储，支持后续通过 send_message 继续对话
  3. 可选自定义 system prompt：每次调用可覆盖默认 prompt

设计要点：
  1. 子 Agent 是一个全新的 Agent 实例（独立消息历史）
  2. 子 Agent 继承父 Agent 的 LLM 和工具集（但排除 agent/send_message 工具本身，防止递归）
  3. 子 Agent 有自己的 system prompt，聚焦于完成单一任务
  4. 子 Agent 的 max_turns 默认较低（防止子任务失控）
  5. 并发安全：多个子 Agent 可以同时运行（各自独立）
  6. 子 Agent 完成后注册到注册表，可通过 send_message 继续对话
"""

import time
import uuid
from typing import Any

from agent.coordinator import WORKER_SYSTEM_PROMPT, format_task_notification


# 子 Agent 的默认 system prompt
_SUB_AGENT_SYSTEM = """你是一个专注执行子任务的 Agent。

你的职责：
- 完整地完成分配给你的任务
- 使用可用的工具来获取信息和执行操作
- 完成后给出清晰、简洁的结果摘要

注意：
- 你是一个子 Agent，由主 Agent 启动来处理特定子任务
- 专注于手头的任务，不要偏离
- 完成任务后直接汇报结果，不需要询问后续步骤"""


class AgentTool:
    """
    子 Agent 工具。

    主 Agent 可以通过此工具启动一个独立的子 Agent，
    子 Agent 拥有独立消息历史，完成任务后返回结构化结果。

    注意：这不是在 __init__.py 中注册的常规工具。
    它由 Agent 类在初始化时自动创建和注入，
    因为它需要运行时的 LLM 和工具配置。
    """

    def __init__(self, llm, tools, system=None, max_turns=10,
                 enable_budget=True, enable_permission=False,
                 agent_registry=None, coordinator_mode=False):
        """
        初始化子 Agent 工具。

        Args:
            llm:       LLM 后端实例（与父 Agent 共享）
            tools:     子 Agent 可用的工具列表（应排除 agent/send_message 工具本身）
            system:    子 Agent 的默认 system prompt（None 则使用内置默认）
            max_turns: 子 Agent 的最大 turn 数（默认 10，比父 Agent 更保守）
            enable_budget: 是否启用 budget 控制
            enable_permission: 是否启用权限检查
            agent_registry: 子 Agent 注册表（主 Agent 提供的共享字典）
            coordinator_mode: 是否为 Coordinator 模式（影响输出格式）
        """
        self._llm = llm
        self._tools = tools
        self._system = system or _SUB_AGENT_SYSTEM
        self._max_turns = max_turns
        self._enable_budget = enable_budget
        self._enable_permission = enable_permission
        self._agent_registry = agent_registry if agent_registry is not None else {}
        self._coordinator_mode = coordinator_mode

    @property
    def name(self) -> str:
        return "agent"

    @property
    def description(self) -> str:
        return """启动一个独立的子 Agent 来执行子任务。

子 Agent 拥有独立的消息历史和工具调用能力，适合处理：
- 需要多步推理的复杂子任务
- 可以独立完成的搜索、分析任务
- 需要并行处理的多个独立任务

使用建议：
- prompt 应清晰描述任务目标和预期输出
- description 用 3-5 个词概括任务
- 子 Agent 完成后返回结构化结果（含 agentId，可用 send_message 继续对话）
- 多个子 Agent 可以并行启动（如同时搜索多个代码区域）

注意：不要用子 Agent 做简单的单步操作（直接调用工具更高效）。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "子 Agent 要执行的任务描述（详细说明任务目标和期望输出）",
                },
                "description": {
                    "type": "string",
                    "description": "任务的简短描述（3-5 个词），用于日志和追踪",
                },
                "system": {
                    "type": "string",
                    "description": "自定义 system prompt（可选，覆盖默认的子 Agent 提示）",
                },
            },
            "required": ["prompt"],
        }

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True  # 每个子 Agent 是完全独立的实例

    def to_api_format(self) -> dict[str, Any]:
        """转换为 API 工具定义格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def call(self, input: dict[str, Any]) -> str:
        """
        启动子 Agent 执行任务，返回结构化结果。

        创建一个全新的 Agent 实例（独立消息历史），
        执行给定的 prompt，返回包含 status、agentId、content、usage 的结构化结果。
        """
        # 延迟导入，避免循环引用
        from agent.agent import Agent

        prompt = input.get("prompt", "")
        description = input.get("description", "子任务")
        custom_system = input.get("system")

        if not prompt:
            return "错误：prompt 参数不能为空"

        # 生成唯一 Agent ID
        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        start_time = time.monotonic()

        # 确定 system prompt
        # 优先级：调用时指定 > Coordinator Worker 默认 > 普通默认
        if custom_system:
            system = custom_system
        elif self._coordinator_mode:
            system = WORKER_SYSTEM_PROMPT
        else:
            system = self._system

        # 创建子 Agent（独立消息历史，不启用子 Agent 工具防止递归）
        sub_agent = Agent(
            llm=self._llm,
            tools=self._tools,
            system=system,
            max_turns=self._max_turns,
            enable_budget=self._enable_budget,
            enable_compact=False,  # 子任务通常较短，不需要压缩
            enable_retry=True,
            enable_permission=self._enable_permission,
            _enable_agent_tool=False,  # 防止递归嵌套
        )

        # 执行子 Agent
        response = sub_agent.chat(prompt)

        # 计算耗时
        duration_ms = int((time.monotonic() - start_time) * 1000)

        # 注册子 Agent，后续可通过 send_message 继续对话
        self._agent_registry[agent_id] = sub_agent

        # 构建结构化结果
        content = response.text or "（子 Agent 未返回文本结果）"

        # Coordinator 模式：使用 <task-notification> XML 格式
        # 普通模式：使用纯文本格式
        if self._coordinator_mode:
            return format_task_notification(
                agent_id=agent_id,
                description=description,
                status="completed",
                content=content,
                turns=sub_agent.turn_count,
                tool_uses=sub_agent._tool_use_count,
                duration_ms=duration_ms,
                total_input_tokens=sub_agent._total_input_tokens,
                total_output_tokens=sub_agent._total_output_tokens,
            )
        else:
            result_parts = [
                f"[子 Agent: {description}]",
                f"agentId: {agent_id}",
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
