"""
Agent 类 —— 管理单次会话的消息历史 + Tool Use 循环

对应 reference 中的 QueryEngine，核心职责：
1. 维护 messages 列表（跨轮上下文记忆）
2. 实现 Tool Use 循环：LLM 请求 → 执行工具 → 回传结果 → 继续

Tool Use 循环的本质：
  while True:
    response = LLM.chat(messages, tools)
    if response.stop_reason == "end_turn":
      return response.text
    elif response.has_tool_use:
      for tool_use in response.tool_uses:
        result = execute_tool(tool_use)
        messages.append(tool_result)
      continue  # 下一个 turn

流式输出：
  stream() 方法 yield StreamEvent 字典，调用方根据 type 决定如何处理：
  - {"type": "text", "text": "..."}              — 文本片段
  - {"type": "tool_use_start", "name": "..."}    — 开始执行工具
  - {"type": "tool_use_end", "result": "..."}    — 工具执行完成
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Iterator

from agent.llm.base import BaseLLM, LLMResponse
from agent.llm.anthropic_llm import AnthropicLLM
from agent.llm.openai_llm import build_tool_result_message as build_openai_tool_result
from agent.tools import Tool, find_tool, to_api_tools
from agent.budget import truncate_tool_result, check_context_budget
from agent.compact import maybe_compact
from agent.retry import with_retry, safe_tool_call, RetryError
from agent.permission import PermissionManager, PermissionConfig, get_default_permission_config
from agent.coordinator import (
    COORDINATOR_SYSTEM_PROMPT,
    build_coordinator_context,
)
from agent.session import SessionManager
from agent.hooks import HookManager
from agent.session_memory import SessionMemory
from agent.auto_memory import AutoMemory
from agent.tools.plan_mode import (
    EnterPlanModeTool,
    ExitPlanModeTool,
    is_tool_readonly,
)


# ============================================================================
# StreamEvent —— 流式输出的事件类型
# ============================================================================

@dataclass
class StreamEvent:
    """
    流式事件。

    type 字段区分事件类型：
      - "text": 文本片段，text 字段包含内容
      - "tool_use_start": 开始执行工具，name/input 字段
      - "tool_use_end": 工具执行完成，result 字段
      - "usage": 流结束后的 token 用量，input_tokens/output_tokens/cache_* 字段
    """
    type: str
    text: str = ""
    name: str = ""
    input: dict | None = None
    result: str = ""
    # usage 事件字段（Prompt Caching）
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典，方便调用方处理。"""
        d = {"type": self.type}
        if self.text:
            d["text"] = self.text
        if self.name:
            d["name"] = self.name
        if self.input is not None:
            d["input"] = self.input
        if self.result:
            d["result"] = self.result
        if self.type == "usage":
            d["input_tokens"] = self.input_tokens
            d["output_tokens"] = self.output_tokens
            if self.cache_read_tokens:
                d["cache_read_tokens"] = self.cache_read_tokens
            if self.cache_write_tokens:
                d["cache_write_tokens"] = self.cache_write_tokens
        return d


# ============================================================================
# Agent 类
# ============================================================================

class Agent:
    """
    单次会话的 Agent，支持 Tool Use。

    维护消息历史，实现 query loop：
      - 发送消息给 LLM（带上 tools 参数）
      - 如果 LLM 请求工具：执行工具，回传结果，继续
      - 如果 LLM 直接回答：返回结果
    """

    def __init__(
        self,
        llm: BaseLLM,
        tools: list[Tool] | None = None,
        system: str | None = None,
        max_turns: int = 20,
        enable_budget: bool = True,
        enable_compact: bool = True,
        max_tool_result_length: int = 10000,
        compact_threshold: int = 80000,
        enable_retry: bool = True,
        max_retries: int = 3,
        permission_config: PermissionConfig | None = None,
        enable_permission: bool = True,
        coordinator_mode: bool = False,
        session_manager: SessionManager | None = None,
        hook_manager: HookManager | None = None,
        session_memory: SessionMemory | None = None,
        auto_memory: AutoMemory | None = None,
        _enable_agent_tool: bool = True,
    ):
        """
        初始化 Agent。

        Args:
            llm:       LLM 后端实例
            tools:     可用工具列表（None 则使用内置工具）
            system:    系统提示（可选）
            max_turns: 最大 turn 数，防止无限循环
            enable_budget: 是否启用 tool result 预算控制
            enable_compact: 是否启用自动压缩
            max_tool_result_length: tool result 最大长度
            compact_threshold: 触发压缩的 token 阈值
            enable_retry: 是否启用 LLM 调用重试
            max_retries: LLM 调用最大重试次数
            permission_config: 权限配置
            enable_permission: 是否启用权限检查
            coordinator_mode: 是否启用 Coordinator 模式（主 Agent 编排子 Agent）
            session_manager: 会话持久化管理器（None 则不持久化）
            hook_manager: Hooks 管理器（None 则不执行 Hook）
            _enable_agent_tool: 是否自动添加子 Agent 工具（内部参数，子 Agent 设为 False 防止递归）
        """
        self.llm = llm
        self.tools = tools or []
        self.max_turns = max_turns
        self.enable_budget = enable_budget
        self.enable_compact = enable_compact
        self.max_tool_result_length = max_tool_result_length
        self.compact_threshold = compact_threshold
        self.enable_retry = enable_retry
        self.max_retries = max_retries
        self.enable_permission = enable_permission
        self.coordinator_mode = coordinator_mode
        self.session_manager = session_manager
        self.hook_manager = hook_manager
        self.session_memory = session_memory
        self.auto_memory = auto_memory

        # Plan Mode 状态
        self.plan_mode = False

        # Coordinator 模式：将 coordinator 系统提示与用户提供的系统提示合并
        if coordinator_mode:
            coordinator_ctx = build_coordinator_context(
                [t.name for t in self.tools]
            )
            base = COORDINATOR_SYSTEM_PROMPT + coordinator_ctx
            self.system = base + "\n\n" + system if system else base
        else:
            self.system = system
        # 权限管理器
        if permission_config:
            self.permission_manager = PermissionManager(permission_config)
        else:
            self.permission_manager = PermissionManager(get_default_permission_config())
        # 消息历史：[{"role": "user"|"assistant", "content": "..."}]
        # 对应 reference QueryEngine.mutableMessages
        self.messages: list[dict] = []
        # 判断是否为 Anthropic 后端（用于选择消息格式）
        self._is_anthropic = isinstance(llm, AnthropicLLM)

        # 统计信息
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._tool_use_count: int = 0
        # 子 Agent 注册表
        # key: agent_id, value: Agent 实例
        # 主 Agent 保存所有子 Agent，支持后续通过 send_message 继续对话
        self._sub_agent_registry: dict[str, "Agent"] = {}

        # 自动添加子 Agent 工具 + SendMessage
        # 子 Agent 自身不添加此工具，防止无限递归
        if _enable_agent_tool:
            from agent.tools.agent_tool import AgentTool
            from agent.tools.send_message import SendMessageTool
            # 子 Agent 可用的工具 = 当前工具集（不含 agent 和 send_message 工具本身）
            sub_tools = [t for t in self.tools if t.name not in ("agent", "send_message")]
            agent_tool = AgentTool(
                llm=llm,
                tools=sub_tools,
                max_turns=min(max_turns, 10),  # 子 Agent 的 max_turns 更保守
                enable_budget=enable_budget,
                enable_permission=enable_permission,
                agent_registry=self._sub_agent_registry,
                coordinator_mode=coordinator_mode,
            )
            send_message_tool = SendMessageTool(
                agent_registry=self._sub_agent_registry,
            )
            self.tools.append(agent_tool)
            self.tools.append(send_message_tool)

            # Plan Mode 工具（仅主 Agent 添加）
            self.tools.append(EnterPlanModeTool())
            self.tools.append(ExitPlanModeTool())

    def _get_tool_argument(self, tool_use: dict) -> str:
        """
        获取工具参数的字符串表示（用于权限检查）。
        """
        tool_name = tool_use.get("name", "")
        tool_input = tool_use.get("input", {})

        if tool_name == "bash":
            return tool_input.get("command", "")
        elif tool_name == "read":
            return tool_input.get("file_path", "")
        elif tool_name == "write":
            return tool_input.get("file_path", "")
        elif tool_name == "edit":
            return tool_input.get("file_path", "")
        elif tool_name == "glob":
            return tool_input.get("pattern", "")
        elif tool_name == "grep":
            return tool_input.get("pattern", "")
        else:
            # 默认返回整个 input 的字符串表示
            return str(tool_input)

    def _execute_tool(self, tool_use: dict) -> str:
        """
        执行单个工具调用（带错误处理、权限检查和 Hooks）。

        Args:
            tool_use: {"id": "...", "name": "...", "input": {...}}

        Returns:
            工具执行结果字符串（已应用 budget 控制）
        """
        tool_name = tool_use.get("name", "")
        tool_input = tool_use.get("input", {})

        # 权限检查
        if self.enable_permission:
            argument = self._get_tool_argument(tool_use)
            allowed, reason = self.permission_manager.should_execute(tool_name, argument)
            if not allowed:
                return f"错误：权限拒绝 - {reason}"

        # Plan Mode 检查：规划模式下只允许只读操作
        if self.plan_mode and not is_tool_readonly(tool_name, tool_input):
            return (
                f"错误：规划模式下不允许执行 '{tool_name}'（只读工具可用）。"
                "请先调用 exit_plan_mode 提交方案后再执行修改操作。"
            )

        # PreToolUse Hook
        if self.hook_manager:
            pre_result = self.hook_manager.run_pre_tool_use(tool_name, tool_input)
            if pre_result.is_blocked:
                return f"错误：被 Hook 阻止 - {pre_result.block_reason}"
            if pre_result.updated_input is not None:
                tool_input = pre_result.updated_input

        tool = find_tool(tool_name, self.tools)
        if tool is None:
            return f"错误：未知工具 '{tool_name}'"

        # 使用 safe_tool_call 包装工具执行
        result = safe_tool_call(
            tool.call,
            tool_input,
            default_error_message=f"工具 '{tool_name}' 执行失败",
        )

        # Plan Mode 状态切换（在工具成功执行后修改，避免 Hook 阻止后状态不一致）
        if tool_name == "enter_plan_mode":
            self.plan_mode = True
        elif tool_name == "exit_plan_mode":
            self.plan_mode = False

        # 应用 budget 控制
        if self.enable_budget:
            result = truncate_tool_result(result, self.max_tool_result_length)

        # 记录工具调用（Session Memory 计数器）
        if self.session_memory:
            self.session_memory.record_tool_call()

        # PostToolUse Hook
        if self.hook_manager:
            post_result = self.hook_manager.run_post_tool_use(
                tool_name, tool_input, result
            )
            if post_result.additional_contexts:
                context = "\n".join(post_result.additional_contexts)
                result = result + f"\n\n[Hook context] {context}"

        return result

    def _is_tool_concurrency_safe(self, tool_use: dict) -> bool:
        """
        判断一次工具调用是否可以并发执行。
        查找工具实例，调用其 is_concurrency_safe() 方法。
        """
        tool_name = tool_use.get("name", "")
        tool_input = tool_use.get("input", {})
        tool = find_tool(tool_name, self.tools)
        if tool is None:
            return False
        return tool.is_concurrency_safe(tool_input)

    def _partition_tool_calls(
        self,
        tool_uses: list[dict],
    ) -> list[list[int]]:
        """
        将工具调用分区为可并发执行的批次。

        规则：
          - 连续的并发安全工具归入同一批次（并发执行）
          - 非并发安全工具独占一个批次（串行执行）

        示例：
          输入: [read, glob, grep, write, read, read]
          安全: [True, True, True, False, True, True]
          分区: [[0,1,2], [3], [4,5]]

        Returns:
            批次列表，每个批次包含 tool_uses 的索引
        """
        if not tool_uses:
            return []

        batches: list[list[int]] = []
        current_batch: list[int] = []
        current_is_safe = None

        for i, tool_use in enumerate(tool_uses):
            is_safe = self._is_tool_concurrency_safe(tool_use)

            if current_is_safe is None:
                # 第一个工具
                current_batch = [i]
                current_is_safe = is_safe
            elif is_safe and current_is_safe:
                # 连续的并发安全工具，合并到当前批次
                current_batch.append(i)
            else:
                # 安全性发生变化，或者当前是非安全的 → 结束当前批次
                batches.append(current_batch)
                current_batch = [i]
                current_is_safe = is_safe

        if current_batch:
            batches.append(current_batch)

        return batches

    def _execute_tools(self, tool_uses: list[dict]) -> list[str]:
        """
        执行工具调用，对并发安全的工具使用线程池并发执行。

        对应 reference 中 toolOrchestration.ts 的 runTools()。

        流程：
          1. 分区工具调用（_partition_tool_calls）
          2. 每个批次中：
             - 单个工具：直接串行执行
             - 多个并发安全工具：使用 ThreadPoolExecutor 并发执行
          3. 结果按原始顺序返回

        Returns:
            工具执行结果列表，顺序与 tool_uses 一致
        """
        results = [None] * len(tool_uses)
        batches = self._partition_tool_calls(tool_uses)

        for batch in batches:
            if len(batch) == 1:
                # 单个工具，直接执行
                idx = batch[0]
                results[idx] = self._execute_tool(tool_uses[idx])
            else:
                # 多个并发安全工具，使用线程池
                with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                    future_to_idx = {
                        executor.submit(self._execute_tool, tool_uses[idx]): idx
                        for idx in batch
                    }
                    for future in as_completed(future_to_idx):
                        idx = future_to_idx[future]
                        try:
                            results[idx] = future.result()
                        except Exception as e:
                            results[idx] = f"错误：工具并发执行失败 - {e}"

        return results

    def _build_tool_result_messages(
        self,
        tool_uses: list[dict],
        results: list[str],
    ) -> list[dict]:
        """
        构建 tool_result 消息，根据 LLM 类型选择格式。

        Anthropic 格式（单条 user 消息，包含多个 tool_result blocks）：
            {"role": "user", "content": [{"type": "tool_result", ...}, ...]}

        OpenAI 格式（每个工具调用一条 tool 消息）：
            {"role": "tool", "tool_call_id": "...", "content": "..."}
        """
        if self._is_anthropic:
            # Anthropic: 单条 user 消息包含所有 tool_result blocks
            tool_result_blocks = []
            for tool_use, result in zip(tool_uses, results):
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.get("id", ""),
                    "content": result,
                })
            return [{"role": "user", "content": tool_result_blocks}]

        else:
            # OpenAI: 每个工具调用一条 tool 消息
            messages = []
            for tool_use, result in zip(tool_uses, results):
                messages.append(build_openai_tool_result(
                    tool_use_id=tool_use.get("id", ""),
                    content=result,
                ))
            return messages

    def _build_assistant_message(self, response: LLMResponse) -> dict:
        """
        构建 assistant 消息，根据 LLM 类型选择格式。

        Anthropic 格式：
            {"role": "assistant", "content": [content blocks]}

        OpenAI 格式（需要特殊处理 tool_calls）：
            {"role": "assistant", "content": text 或 None, "tool_calls": [...]}
        """
        if self._is_anthropic:
            return {"role": "assistant", "content": response.content}

        else:
            # OpenAI 格式
            text_content = response.text or None
            tool_calls = []

            for tool_use in response.tool_uses:
                tool_calls.append({
                    "id": tool_use.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tool_use.get("name", ""),
                        "arguments": json.dumps(tool_use.get("input", {})),
                    }
                })

            msg = {"role": "assistant"}
            if text_content:
                msg["content"] = text_content
            if tool_calls:
                msg["tool_calls"] = tool_calls

            return msg

    def _check_and_compact(self) -> None:
        """
        检查上下文预算，如果需要则执行压缩。
        同时检查 Session Memory 是否需要更新。
        """
        # Session Memory：定期更新笔记（独立于压缩开关）
        if self.session_memory:
            self.session_memory.maybe_update(self.messages)

        if not self.enable_compact:
            return

        # 检查是否需要压缩
        budget_info = check_context_budget(self.messages, self.compact_threshold)
        if budget_info["is_warning"]:
            # 执行压缩
            new_messages, compact_result = maybe_compact(
                self.messages,
                self.llm,
                threshold=self.compact_threshold,
            )
            if compact_result:
                self.messages = new_messages

                # 压缩后注入 Session Memory 笔记，防止关键信息丢失
                if self.session_memory:
                    notes = self.session_memory.get_notes_for_injection()
                    if notes:
                        for i, msg in enumerate(self.messages):
                            if msg.get("role") == "user" and "[历史摘要]" in msg.get("content", ""):
                                self.messages[i]["content"] += "\n\n" + notes
                                break

    def _call_llm_with_retry(self, api_tools: list[dict] | None) -> LLMResponse:
        """
        调用 LLM，带重试机制。
        """
        if self.enable_retry:
            @with_retry(
                max_retries=self.max_retries,
                retryable_exceptions=(ConnectionError, TimeoutError, Exception),
            )
            def _call():
                return self.llm.chat(
                    self.messages,
                    system=self.system,
                    tools=api_tools,
                )
            return _call()
        else:
            return self.llm.chat(
                self.messages,
                system=self.system,
                tools=api_tools,
            )

    def _run_hook_user_prompt(self, prompt: str) -> str:
        """
        执行 UserPromptSubmit Hook，返回可能追加了 Hook context 的 prompt。
        """
        if not self.hook_manager:
            return prompt
        result = self.hook_manager.run_user_prompt_submit(prompt)
        if result.additional_contexts:
            context = "\n".join(result.additional_contexts)
            return prompt + f"\n\n[Hook context] {context}"
        return prompt

    def _run_tool_loop(self, prompt: str) -> LLMResponse:
        """
        执行完整的 Tool Use 循环（非流式）。

        流程：
          1. 把用户输入追加到 messages
          2. 检查并执行压缩
          3. 调用 LLM（带重试）
          4. 如果有 tool_use：执行工具，追加 tool_result，回到步骤 2
          5. 如果 end_turn：返回最终响应
          6. 达到 max_turns：返回错误

        Returns:
            LLMResponse 包含最终文本和 token 用量
        """
        # UserPromptSubmit Hook（可能追加 context）
        prompt = self._run_hook_user_prompt(prompt)

        # 追加用户消息
        self.messages.append({"role": "user", "content": prompt})
        self._persist_message(self.messages[-1])

        # 检查并执行压缩
        self._check_and_compact()

        # 准备 tools 参数
        api_tools = to_api_tools(self.tools) if self.tools else None

        turn_count = 0
        while turn_count < self.max_turns:
            turn_count += 1

            try:
                # 调用 LLM（带重试）
                response = self._call_llm_with_retry(api_tools)
            except RetryError as e:
                # 重试耗尽，返回错误
                return LLMResponse(
                    content=[{"type": "text", "text": f"[错误] LLM 调用失败: {e}"}],
                    input_tokens=0,
                    output_tokens=0,
                    model=self.llm.model,
                    stop_reason="error",
                )

            # 累加 token 统计
            self._total_input_tokens += response.input_tokens
            self._total_output_tokens += response.output_tokens

            # 追加 assistant 消息（使用对应格式）
            assistant_msg = self._build_assistant_message(response)
            self.messages.append(assistant_msg)
            self._persist_message(assistant_msg)

            # 如果没有工具调用，返回结果
            if not response.has_tool_use:
                # Auto-Memory：对话结束时提取持久性记忆
                self._maybe_extract_memories()
                return response

            # 累加工具调用次数
            self._tool_use_count += len(response.tool_uses)

            # 执行所有工具调用（支持并发）
            results = self._execute_tools(response.tool_uses)

            # 追加 tool_result 消息（使用对应格式）
            tool_result_messages = self._build_tool_result_messages(
                response.tool_uses, results
            )
            self.messages.extend(tool_result_messages)
            self._persist_messages(tool_result_messages)

        # 达到 max_turns 限制
        return LLMResponse(
            content=[{"type": "text", "text": f"[错误] 达到最大 turn 数限制 ({self.max_turns})，可能是无限循环"}],
            input_tokens=0,
            output_tokens=0,
            model=self.llm.model,
            stop_reason="max_turns",
        )

    def chat(self, prompt: str) -> LLMResponse:
        """
        发送一轮消息，执行完整的 Tool Use 循环，返回最终响应。

        流式版本请使用 stream()。
        """
        response = self._run_tool_loop(prompt)
        return response

    def stream(self, prompt: str) -> Iterator[StreamEvent]:
        """
        发送一轮消息，流式 yield StreamEvent。

        事件类型：
          - {"type": "text", "text": "..."}           — 文本片段
          - {"type": "tool_use_start", "name": ...}   — 开始执行工具
          - {"type": "tool_use_end", "result": ...}   — 工具执行完成

        调用方示例：
            for event in agent.stream("问题"):
                if event.type == "text":
                    print(event.text, end="", flush=True)
                elif event.type == "tool_use_start":
                    print(f"\\n[调用工具: {event.name}]")
                elif event.type == "tool_use_end":
                    # 可以选择显示结果或静默
                    pass
        """
        # UserPromptSubmit Hook（可能追加 context）
        prompt = self._run_hook_user_prompt(prompt)

        # 追加用户消息
        self.messages.append({"role": "user", "content": prompt})
        self._persist_message(self.messages[-1])

        # 检查并执行压缩
        self._check_and_compact()

        # 准备 tools 参数
        api_tools = to_api_tools(self.tools) if self.tools else None

        turn_count = 0
        final_response = None

        while turn_count < self.max_turns:
            turn_count += 1

            try:
                # 调用 LLM（带重试）
                response = self._call_llm_with_retry(api_tools)
            except RetryError as e:
                # 重试耗尽
                yield StreamEvent(
                    type="text",
                    text=f"\n[错误] LLM 调用失败: {e}",
                )
                return

            # 累加 token 统计
            self._total_input_tokens += response.input_tokens
            self._total_output_tokens += response.output_tokens

            # 追加 assistant 消息
            assistant_msg = self._build_assistant_message(response)
            self.messages.append(assistant_msg)
            self._persist_message(assistant_msg)

            # 先 yield 文本内容
            if response.text:
                yield StreamEvent(type="text", text=response.text)

            # 如果没有工具调用，结束循环
            if not response.has_tool_use:
                final_response = response
                # Auto-Memory：对话结束时提取持久性记忆
                self._maybe_extract_memories()
                break

            # 累加工具调用次数
            self._tool_use_count += len(response.tool_uses)

            # 执行所有工具调用（支持并发）
            batches = self._partition_tool_calls(response.tool_uses)
            results = [None] * len(response.tool_uses)

            for batch in batches:
                # 先 yield 当前批次所有工具的开始事件
                for idx in batch:
                    tool_use = response.tool_uses[idx]
                    yield StreamEvent(
                        type="tool_use_start",
                        name=tool_use.get("name", ""),
                        input=tool_use.get("input"),
                    )

                if len(batch) == 1:
                    # 单个工具，直接执行
                    idx = batch[0]
                    results[idx] = self._execute_tool(response.tool_uses[idx])
                    yield StreamEvent(type="tool_use_end", result=results[idx])
                else:
                    # 多个并发安全工具，使用线程池
                    with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                        future_to_idx = {
                            executor.submit(
                                self._execute_tool, response.tool_uses[idx]
                            ): idx
                            for idx in batch
                        }
                        for future in as_completed(future_to_idx):
                            idx = future_to_idx[future]
                            try:
                                results[idx] = future.result()
                            except Exception as e:
                                results[idx] = f"错误：工具并发执行失败 - {e}"

                    # 按原始顺序 yield 结束事件
                    for idx in batch:
                        yield StreamEvent(type="tool_use_end", result=results[idx])

            # 追加 tool_result 消息
            tool_result_messages = self._build_tool_result_messages(
                response.tool_uses, results
            )
            self.messages.extend(tool_result_messages)
            self._persist_messages(tool_result_messages)

        # 达到 max_turns 限制
        if final_response is None:
            yield StreamEvent(
                type="text",
                text=f"\n[错误] 达到最大 turn 数限制 ({self.max_turns})，可能是无限循环",
            )
        elif final_response:
            # 流结束前，报告 token 用量
            yield StreamEvent(
                type="usage",
                input_tokens=final_response.input_tokens,
                output_tokens=final_response.output_tokens,
                cache_read_tokens=final_response.cache_read_tokens,
                cache_write_tokens=final_response.cache_write_tokens,
            )

    def _persist_message(self, message: dict) -> None:
        """将消息持久化到会话文件（如果启用了 session_manager）。"""
        if self.session_manager:
            self.session_manager.append_message(message)

    def _persist_messages(self, messages: list[dict]) -> None:
        """批量持久化消息到会话文件。"""
        if self.session_manager:
            self.session_manager.append_messages(messages)

    def _maybe_extract_memories(self) -> None:
        """
        对话结束时提取持久性记忆（Auto-Memory）。

        在 LLM 最终回复（无工具调用）后调用。
        静默执行，失败不影响主流程。
        """
        if not self.auto_memory:
            return
        try:
            self.auto_memory.extract_and_save(self.messages)
        except Exception:
            pass

    def restore_messages(self, messages: list[dict]) -> None:
        """
        从持久化存储恢复消息历史（用于 --resume）。

        Args:
            messages: 从 SessionManager.load_messages() 加载的消息列表
        """
        self.messages = messages

    def clear(self):
        """清空消息历史，开始新的会话。"""
        self.messages.clear()

    @property
    def turn_count(self) -> int:
        """当前已完成的对话轮数（一问一答算一轮）。"""
        count = 0
        for m in self.messages:
            if m["role"] != "user":
                continue
            content = m.get("content")
            # Anthropic 的 tool_result 也是 role=user，需要排除
            if isinstance(content, list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            ):
                continue
            count += 1
        return count
