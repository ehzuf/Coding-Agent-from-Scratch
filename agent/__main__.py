"""
CLI 入口

功能：
  - 支持流式/非流式模式
  - 显示 token 用量和 prompt cache 命中/写入信息
  - build_agent 中为 Anthropic 后端传入 enable_cache 参数

配置来源：
  1. 命令行参数（最高优先级）
  2. 环境变量（LLM_PROVIDER, ANTHROPIC_MODEL 等）
  3. 默认值
"""

import argparse
import os
import sys

# 启用 readline 支持，改善终端输入体验（包括中文输入）
try:
    import readline
except ImportError:
    pass

from agent.llm import create_llm
from agent.agent import Agent, StreamEvent
from agent.tools import get_tools
from agent.context import build_system_prompt, get_context_info
from agent.config import load_config, get_default_model
from agent.permission import (
    PermissionConfig,
    get_default_permission_config,
    get_permissive_permission_config,
    get_strict_permission_config,
)
from agent.session import SessionManager
from agent.hooks import HookManager
from agent.session_memory import SessionMemory
from agent.auto_memory import AutoMemory
from agent.mcp_client import MCPManager
from agent.skills import SkillManager
from agent.skill_evolution import SkillEvolution

def build_agent(args) -> tuple[Agent, "Config"]:
    """根据配置构建 Agent 实例，返回 (agent, config) 元组。"""
    # 加载配置（命令行参数 > 环境变量 > 默认值）
    config = load_config(args)

    provider = config.provider
    model = config.model or get_default_model(provider)

    try:
        llm = create_llm(
            provider=provider,
            model=model,
            base_url=config.base_url,
        )
    except Exception as e:
        print(f"[错误] 初始化 LLM 失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 加载内置工具
    tools = get_tools()

    # MCP Client：连接 MCP Server，发现远程工具
    mcp_manager = MCPManager(cwd=config.cwd)
    if mcp_manager.has_servers():
        mcp_tools = mcp_manager.connect_all()
        if mcp_tools:
            tools.extend(mcp_tools)

    # Skills 系统：加载自定义 Skill
    skill_manager = SkillManager(cwd=config.cwd)
    if skill_manager.has_skills():
        skill_tool = skill_manager.create_skill_tool(
            llm=llm, parent_tools=tools, max_turns=min(config.max_turns, 10),
        )
        tools.append(skill_tool)

    # 构建系统提示（包含 AGENTS.md 和上下文信息）
    system_prompt = build_system_prompt(
        base_system=config.system_prompt,
        cwd=config.cwd,
        include_date=True,
    )

    # 选择权限配置
    if not config.enable_permission:
        # 禁用权限检查
        permission_config = get_permissive_permission_config()
    elif config.permission_mode == "allow":
        permission_config = get_permissive_permission_config()
    elif config.permission_mode == "strict":
        permission_config = get_strict_permission_config()
    else:
        permission_config = get_default_permission_config()

    # 会话持久化
    session_manager = None
    if config.enable_session:
        cwd_abs = os.path.abspath(config.cwd)
        if config.resume_session:
            # 恢复已有会话
            try:
                session_manager = SessionManager.resume(config.resume_session)
                print(f"  恢复会话: {config.resume_session}")
            except FileNotFoundError as e:
                print(f"[错误] {e}", file=sys.stderr)
                sys.exit(1)
        else:
            # 创建新会话
            session_manager = SessionManager(
                project=cwd_abs,
                model=model,
            )

    # 显示上下文信息
    context_info = get_context_info(config.cwd)
    print(f"[{provider} / {model}] 工作目录: {context_info['cwd']}")
    memory_files = context_info["memory_files"]
    if memory_files:
        print(f"  记忆文件: 已加载 {len(memory_files)} 个")
        for mf in memory_files:
            print(f"    [{mf.memory_type}] {mf.source}")
    if tools:
        print(f"  可用工具: {[t.name for t in tools]}")
    print(f"  权限模式: {config.permission_mode}")
    if config.coordinator_mode:
        print(f"  模式: Coordinator（编排子 Agent 执行任务）")
    if session_manager:
        print(f"  会话 ID: {session_manager.session_id}")
    if mcp_manager.connections:
        total_mcp_tools = sum(len(c.tools) for c in mcp_manager.connections.values())
        server_names = list(mcp_manager.connections.keys())
        print(f"  MCP: {len(server_names)} 个 Server 已连接，{total_mcp_tools} 个工具 ({server_names})")
    if skill_manager.has_skills():
        print(f"  Skills: {skill_manager.list_skill_names()}")

    # Hooks 管理器
    hook_manager = None
    if config.enable_hooks:
        hook_manager = HookManager(cwd=config.cwd)
        if hook_manager.has_hooks("PreToolUse") or hook_manager.has_hooks("PostToolUse") or hook_manager.has_hooks("UserPromptSubmit"):
            print(f"  Hooks: 已加载")

    # Session Memory（会话记忆）
    session_memory = SessionMemory(llm=llm)

    # Auto-Memory（跨会话记忆）
    auto_memory = AutoMemory(llm=llm, cwd=config.cwd)
    memory_prompt = auto_memory.load_memory_prompt()
    if memory_prompt:
        system_prompt = system_prompt + "\n\n" + memory_prompt if system_prompt else memory_prompt

    # Skill 自进化
    skill_evolution = SkillEvolution(llm=llm)

    # 创建 Agent，传入所有配置
    agent = Agent(
        llm=llm,
        tools=tools,
        system=system_prompt,
        max_turns=config.max_turns,
        enable_budget=config.enable_budget,
        enable_compact=config.enable_compact,
        enable_retry=config.enable_retry,
        max_retries=config.max_retries,
        permission_config=permission_config,
        enable_permission=config.enable_permission,
        coordinator_mode=config.coordinator_mode,
        session_manager=session_manager,
        hook_manager=hook_manager,
        session_memory=session_memory,
        auto_memory=auto_memory,
        skill_evolution=skill_evolution,
    )

    # 存储 MCP 管理器引用（用于 REPL /mcp 命令和关闭）
    agent._mcp_manager = mcp_manager

    # 存储 Skill 管理器引用（用于 REPL /skills 命令）
    agent._skill_manager = skill_manager

    # 恢复会话消息历史
    if config.resume_session and session_manager:
        messages = session_manager.load_messages()
        agent.restore_messages(messages)
        print(f"  已恢复 {len(messages)} 条消息")

    return agent, config


def _format_cache_info(response) -> str:
    """
    格式化 prompt cache 用量信息，用于 token 统计显示。

    如果有 cache 命中或写入，返回类似 ", 缓存命中 1200, 缓存写入 3000" 的字符串。
    如果没有 cache 信息，返回空字符串。
    """
    parts = []
    if response.cache_read_tokens:
        parts.append(f"缓存命中 {response.cache_read_tokens}")
    if response.cache_write_tokens:
        parts.append(f"缓存写入 {response.cache_write_tokens}")
    if parts:
        return ", " + ", ".join(parts)
    return ""


def _format_cache_info_from_event(event) -> str:
    """
    格式化 StreamEvent (usage 类型) 的 cache 用量信息。
    """
    parts = []
    if event.cache_read_tokens:
        parts.append(f"缓存命中 {event.cache_read_tokens}")
    if event.cache_write_tokens:
        parts.append(f"缓存写入 {event.cache_write_tokens}")
    if parts:
        return ", " + ", ".join(parts)
    return ""


def _estimate_tokens(text: str) -> int:
    """
    简单估算 token 数（约 3 字符 = 1 token）。
    精确计算需要 tokenizer，这里用快速估算。
    """
    if not text:
        return 0
    return len(text) // 3 + 1


def _estimate_message_tokens(msg: dict) -> int:
    """估算单条消息的 token 数。"""
    total = 0
    content = msg.get("content")

    if isinstance(content, str):
        total += _estimate_tokens(content)
    elif isinstance(content, list):
        # content blocks 格式（Anthropic 或包含 tool_result 的消息）
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    total += _estimate_tokens(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    total += _estimate_tokens(block.get("content", ""))
                elif block.get("type") == "tool_use":
                    total += _estimate_tokens(str(block.get("input", {})))

    # OpenAI 格式的 tool_calls（content 可能为 None，token 在 tool_calls 里）
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            if isinstance(tc, dict):
                func = tc.get("function", {})
                total += _estimate_tokens(func.get("name", ""))
                total += _estimate_tokens(func.get("arguments", ""))

    return total


def _estimate_tools_tokens(tools: list) -> int:
    """估算工具定义的 token 数。"""
    total = 0
    for tool in tools:
        total += _estimate_tokens(tool.name)
        total += _estimate_tokens(tool.description)
        total += _estimate_tokens(str(tool.input_schema))
    return total


def show_cache_info(agent: Agent):
    """
    显示当前缓存分析信息。

    根据 LLM 后端（Anthropic / OpenAI）显示不同的缓存策略说明。
    """
    is_anthropic = agent._is_anthropic

    print("\n=== 缓存分析 ===\n")
    print(f"后端: {'Anthropic（手动标记 cache_control）' if is_anthropic else 'OpenAI（自动缓存相同前缀）'}")

    # 1. System prompt 分析
    system = agent.system or ""
    system_tokens = _estimate_tokens(system)
    print(f"\nSystem prompt: ~{system_tokens} tokens")
    if system_tokens > 0:
        if is_anthropic:
            print(f"  → 已加 cache_control 标记，每次请求都会命中缓存")
        else:
            print(f"  → 自动参与前缀缓存")

    # 2. Tools 分析
    tools_tokens = _estimate_tools_tokens(agent.tools) if agent.tools else 0
    if tools_tokens > 0:
        print(f"Tools 定义:    ~{tools_tokens} tokens（{len(agent.tools)} 个工具）")
        print(f"  → 工具列表不变，自动参与缓存")

    # 3. 消息历史分析
    messages = agent.messages
    if not messages:
        print("\n消息历史: 空（还没有对话）")
        print()
        return

    total_msg_tokens = 0
    msg_tokens_list = []

    for i, msg in enumerate(messages):
        tokens = _estimate_message_tokens(msg)
        total_msg_tokens += tokens
        msg_tokens_list.append((i, msg.get("role", "?"), tokens))

    print(f"\n消息历史: {len(messages)} 条，~{total_msg_tokens} tokens")

    # 确定 cache_control 标记位置（仅 Anthropic 有意义）
    # show_cache_info 在请求后调用，此时最后一条是 assistant。
    # 下次 API 调用会追加一条 user，_add_cache_control_to_messages 直接在 len-1 打标记。
    if len(messages) >= 2:
        cache_idx = len(messages) - 1
    else:
        cache_idx = -1

    print("\n消息分布:")
    for i, role, tokens in msg_tokens_list:
        marker = ""
        if is_anthropic:
            if i == cache_idx:
                marker = " ← cache_control（此消息及之前被缓存）"
            elif cache_idx >= 0 and i < cache_idx:
                marker = " [已缓存]"
        print(f"  [{i:2d}] {role:9s}: ~{tokens:5d} tokens{marker}")

    # 4. 缓存策略
    print("\n缓存策略:")
    total_cached = system_tokens + tools_tokens

    if is_anthropic:
        if cache_idx < 0:
            print(f"  消息数 < 4，仅缓存 system prompt + tools")
            print(f"  对话再深入几轮后，历史消息也会被标记缓存")
        else:
            cached_msg_tokens = sum(t for i, _, t in msg_tokens_list if i <= cache_idx)
            total_cached += cached_msg_tokens
            uncached_msg_tokens = total_msg_tokens - cached_msg_tokens
            uncached_count = len(messages) - cache_idx - 1
            print(f"  已缓存: ~{total_cached} tokens（system {system_tokens} + tools {tools_tokens} + 历史 {cached_msg_tokens}）")
            if uncached_count > 0:
                print(f"  未缓存: ~{uncached_msg_tokens} tokens（最近 {uncached_count} 条消息）")
            else:
                print(f"  下次请求时，仅新输入的 user 消息不在缓存中")
    else:
        print(f"  OpenAI 自动缓存相同前缀，无需手动标记")
        print(f"  缓存对象: system + tools + 对话历史中不变的部分")
        total_cached += total_msg_tokens  # OpenAI 缓存整个前缀
        print(f"  预估可缓存: ~{total_cached} tokens")

    # 5. 费用说明
    print("\n费用参考:")
    if is_anthropic:
        print(f"  缓存命中: 节省 90% 输入费用（仅付 10%）")
        print(f"  缓存写入: 额外 25% 费用（首次建立）")
        print(f"  缓存 TTL: 5 分钟（每次命中重置计时）")
    else:
        print(f"  缓存命中: 节省 50% 输入费用")
        print(f"  缓存自动生效，无额外写入费用")
    print()


def handle_stream_event(event: StreamEvent, verbose: bool = True):
    """
    处理流式事件，打印到终端。

    Args:
        event:   StreamEvent 实例
        verbose: 是否显示工具调用详情
    """
    if event.type == "text":
        # 文本片段，直接打印
        print(event.text, end="", flush=True)

    elif event.type == "tool_use_start" and verbose:
        # 工具调用开始，显示简洁的提示
        # 用灰色显示工具名，不换行（等工具执行完再继续）
        print(f"\033[90m[{event.name}]\033[0m ", end="", flush=True)

    elif event.type == "tool_use_end":
        # 工具调用结束，静默处理
        # 结果会让 LLM 在后续文本中总结
        pass


def run_once(agent: Agent, prompt: str, use_stream: bool):
    """单次问答。"""
    print()
    try:
        if use_stream:
            # 流式模式，支持 Tool Use
            for event in agent.stream(prompt):
                if event.type == "usage":
                    # 流结束，打印 token 用量
                    cache_info = _format_cache_info_from_event(event)
                    print(f"\n--- token 用量: 输入 {event.input_tokens}, 输出 {event.output_tokens}{cache_info} ---")
                else:
                    handle_stream_event(event, verbose=True)
            print()
        else:
            # 非流式模式
            response = agent.chat(prompt)
            print(response.text)
            # 显示 token 用量，包括 prompt cache 信息
            cache_info = _format_cache_info(response)
            print(f"\n--- token 用量: 输入 {response.input_tokens}, 输出 {response.output_tokens}{cache_info} ---")
            if response.stop_reason and response.stop_reason != "end_turn":
                print(f"--- 停止原因: {response.stop_reason} ---")
    except Exception as e:
        print(f"\n[错误] {e}", file=sys.stderr)


def run_repl(agent: Agent, use_stream: bool):
    """
    交互式多轮对话循环（REPL）。

    对应 reference 中的 REPL 主循环，精简版只处理：
    - 正常用户输入 → 发给 LLM，打印回复
    - /clear        → 清空消息历史
    - /exit /quit   → 退出
    - Ctrl+C        → 退出
    """
    print("\n输入问题开始对话，/clear 清空历史，/cache 缓存分析，/session 会话信息，/hooks 查看 Hooks，/memory 查看记忆，/mcp 查看 MCP，/skills 查看技能，/evolve 强制技能审查，/exit 退出\n")

    while True:
        # 显示轮次提示符
        try:
            prompt = input(f"[{agent.turn_count + 1}] 你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not prompt:
            continue

        # 内置指令
        if prompt in ("/exit", "/quit"):
            print("再见！")
            break

        if prompt == "/clear":
            agent.clear()
            print("--- 历史已清空，开始新对话 ---\n")
            continue

        if prompt == "/cache":
            show_cache_info(agent)
            continue

        if prompt == "/session":
            if agent.session_manager:
                meta = agent.session_manager.get_meta()
                print(f"\n--- 当前会话 ---")
                print(f"  会话 ID: {meta.session_id}")
                print(f"  项目: {meta.project}")
                print(f"  模型: {meta.model}")
                print(f"  消息数: {meta.message_count}")
                print(f"  恢复命令: python -m agent --resume {meta.session_id}")
                print()
            else:
                print("\n会话持久化未启用。使用 --no-session 禁用，不使用则默认启用。\n")
            continue

        if prompt == "/hooks":
            if agent.hook_manager:
                print(f"\n{agent.hook_manager.get_hooks_summary()}\n")
            else:
                print("\nHooks 未启用。\n")
            continue

        if prompt == "/memory":
            if agent.auto_memory:
                memories = agent.auto_memory.list_memories()
                memory_dir = agent.auto_memory.get_memory_dir()
                if memories:
                    print(f"\n--- 跨会话记忆 ({memory_dir}) ---")
                    for m in memories:
                        tag = f"[{m.memory_type}] " if m.memory_type else ""
                        desc = f" — {m.description}" if m.description else ""
                        print(f"  {tag}{m.name}{desc}")
                    print(f"\n  共 {len(memories)} 条记忆\n")
                else:
                    print(f"\n暂无跨会话记忆。记忆目录: {memory_dir}\n")
            else:
                print("\nAuto-Memory 未启用。\n")
            continue

        if prompt == "/mcp":
            mcp_mgr = getattr(agent, "_mcp_manager", None)
            if mcp_mgr:
                print(f"\n{mcp_mgr.get_status()}\n")
            else:
                print("\nMCP 未启用。\n")
            continue

        if prompt == "/skills":
            skill_mgr = getattr(agent, "_skill_manager", None)
            if skill_mgr and skill_mgr.has_skills():
                print(f"\n{skill_mgr.get_summary()}\n")
            else:
                print("\n暂无可用 Skill。可在 ~/.coding-agent/skills/ 或 .coding-agent/skills/ 中添加 Skill 文件。\n")
            continue

        if prompt == "/evolve":
            if agent.skill_evolution:
                # 临时设置计数器为阈值，强制触发
                agent.skill_evolution._counter = agent.skill_evolution.threshold
                results = agent.skill_evolution.maybe_evolve(agent.messages)
                if results:
                    for r in results:
                        print(f"  \U0001f4a1 {r}")
                else:
                    print("\n当前对话没有值得沉淀的 Skill。\n")
            else:
                print("\nSkill 自进化未启用。\n")
            continue

        # 正常对话
        print("助手: ", end="", flush=True)
        try:
            if use_stream:
                # 流式模式，支持 Tool Use
                last_usage = None
                for event in agent.stream(prompt):
                    if event.type == "usage":
                        last_usage = event
                    else:
                        handle_stream_event(event, verbose=True)
                if last_usage:
                    cache_info = _format_cache_info_from_event(last_usage)
                    print(f"\n  [输入 {last_usage.input_tokens} / 输出 {last_usage.output_tokens} tokens{cache_info}, 共 {agent.turn_count} 轮]\n")
                else:
                    print(f"\n  [共 {agent.turn_count} 轮]\n")
            else:
                # 非流式模式
                response = agent.chat(prompt)
                print(response.text)
                cache_info = _format_cache_info(response)
                print(f"  [输入 {response.input_tokens} / 输出 {response.output_tokens} tokens{cache_info}]\n")
        except Exception as e:
            print(f"\n[错误] {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Coding Agent CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
环境变量:
  LLM_PROVIDER          默认 LLM 后端 (anthropic/openai)
  ANTHROPIC_MODEL       Anthropic 模型名称
  OPENAI_MODEL          OpenAI 模型名称
  ANTHROPIC_BASE_URL    Anthropic API 地址
  OPENAI_BASE_URL       OpenAI API 地址
  AGENT_MAX_TURNS       最大 turn 数
  AGENT_MAX_RETRIES     最大重试次数
  AGENT_NO_BUDGET       禁用 budget 控制
  AGENT_NO_COMPACT      禁用自动压缩
  AGENT_NO_RETRY        禁用重试
  AGENT_NO_STREAM       禁用流式输出
  AGENT_NO_PERMISSION   禁用权限检查
  AGENT_PERMISSION_MODE 权限模式 (ask/allow/strict)
  AGENT_COORDINATOR     启用 Coordinator 模式
  AGENT_NO_SESSION      禁用会话持久化
  AGENT_RESUME_SESSION  恢复指定会话 ID
  AGENT_NO_HOOKS        禁用 Hooks

权限模式:
  ask    默认模式，危险操作会询问用户确认
  allow  宽松模式，自动允许所有操作（除了最危险的）
  strict 严格模式，自动拒绝所有未明确允许的操作
        """.strip(),
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="单次问答模式：直接传入问题。不传则进入交互式 REPL。",
    )
    parser.add_argument(
        "--provider",
        default=None,
        choices=["anthropic", "openai"],
        help="LLM 后端（默认: anthropic 或 LLM_PROVIDER 环境变量）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="模型名称（不填则读取环境变量或默认值）",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="自定义 API 地址（不填则读取环境变量）",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="禁用流式输出，使用非流式模式",
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="工作目录（默认当前目录），用于查找 AGENTS.md",
    )
    parser.add_argument(
        "--system",
        default=None,
        help="额外的系统提示，会与 AGENTS.md 内容合并",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="最大 turn 数（默认 20）",
    )
    parser.add_argument(
        "--no-budget",
        action="store_true",
        help="禁用 tool result budget 控制",
    )
    parser.add_argument(
        "--no-compact",
        action="store_true",
        help="禁用自动压缩",
    )
    parser.add_argument(
        "--no-retry",
        action="store_true",
        help="禁用 LLM 调用重试",
    )
    parser.add_argument(
        "--permission-mode",
        dest="permission_mode",
        choices=["ask", "allow", "strict"],
        default=None,
        help="权限模式：ask=询问确认, allow=自动允许, strict=自动拒绝",
    )
    parser.add_argument(
        "--no-permission",
        action="store_true",
        help="完全禁用权限检查（等同于 --permission-mode allow）",
    )
    parser.add_argument(
        "--show-cache",
        action="store_true",
        help="请求结束后显示缓存分析（token 分布、缓存策略）",
    )
    parser.add_argument(
        "--coordinator",
        action="store_true",
        help="启用 Coordinator 模式（主 Agent 编排子 Agent 并行执行任务）",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION_ID",
        help="恢复指定会话（从中断处继续）",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="列出所有可恢复的会话",
    )
    parser.add_argument(
        "--no-session",
        action="store_true",
        help="禁用会话持久化",
    )
    parser.add_argument(
        "--no-hooks",
        action="store_true",
        help="禁用 Hooks（工具调用前后的自定义脚本）",
    )
    args = parser.parse_args()

    # 列出会话（不启动 Agent）
    if args.list_sessions:
        cwd_abs = os.path.abspath(args.cwd)
        sessions = SessionManager.list_sessions(project=cwd_abs)
        print(SessionManager.format_session_list(sessions))
        return

    agent, config = build_agent(args)

    try:
        if args.prompt:
            # 单次模式
            run_once(agent, args.prompt, config.stream)
            if args.show_cache:
                show_cache_info(agent)
        else:
            # 交互模式
            run_repl(agent, config.stream)
    finally:
        # 清理 MCP 连接
        mcp_mgr = getattr(agent, "_mcp_manager", None)
        if mcp_mgr:
            mcp_mgr.close_all()


if __name__ == "__main__":
    main()
