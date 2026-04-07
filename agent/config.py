"""
配置系统 —— 统一管理配置来源

对应 reference 中的配置系统

配置优先级（从高到低）：
  1. 命令行参数
  2. 环境变量
  3. 配置文件（~/.coding-agent/config.yaml）
  4. 默认值

支持的配置项：
  - provider: LLM 后端 (anthropic/openai)
  - model: 模型名称
  - base_url: API 地址
  - max_turns: 最大 turn 数
  - enable_budget: 是否启用 budget 控制
  - enable_compact: 是否启用自动压缩
  - enable_retry: 是否启用重试
  - max_retries: 最大重试次数
  - permission_mode: 权限模式 (ask/allow/strict)
  - resume_session: 恢复的会话 ID
  - enable_session: 是否启用会话持久化
  - enable_hooks: 是否启用 Hooks
"""

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Config:
    """Agent 配置"""

    # LLM 配置
    provider: str = "anthropic"
    model: str | None = None
    base_url: str | None = None

    # Agent 行为配置
    max_turns: int = 20
    enable_budget: bool = True
    enable_compact: bool = True
    enable_retry: bool = True
    max_retries: int = 3

    # 输出配置
    stream: bool = True
    verbose: bool = True

    # 路径配置
    cwd: str = "."
    system_prompt: str | None = None

    # 权限配置
    enable_permission: bool = True
    permission_mode: str = "ask"  # ask, allow, strict

    # Coordinator 模式
    coordinator_mode: bool = False

    # 会话持久化
    enable_session: bool = True
    resume_session: str | None = None
    list_sessions: bool = False

    # Hooks
    enable_hooks: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量加载配置"""
        config = cls()

        # LLM 配置
        config.provider = os.environ.get("LLM_PROVIDER", config.provider)

        # 根据 provider 选择对应的环境变量
        if config.provider == "anthropic":
            config.model = os.environ.get("ANTHROPIC_MODEL") or config.model
            config.base_url = os.environ.get("ANTHROPIC_BASE_URL")
        elif config.provider == "openai":
            config.model = os.environ.get("OPENAI_MODEL") or config.model
            config.base_url = os.environ.get("OPENAI_BASE_URL")
        else:
            # 默认情况下，尝试从任意环境变量读取
            config.model = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("OPENAI_MODEL")
            config.base_url = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("OPENAI_BASE_URL")

        # 数值配置
        if max_turns := os.environ.get("AGENT_MAX_TURNS"):
            config.max_turns = int(max_turns)
        if max_retries := os.environ.get("AGENT_MAX_RETRIES"):
            config.max_retries = int(max_retries)

        # 布尔配置
        if os.environ.get("AGENT_NO_BUDGET"):
            config.enable_budget = False
        if os.environ.get("AGENT_NO_COMPACT"):
            config.enable_compact = False
        if os.environ.get("AGENT_NO_RETRY"):
            config.enable_retry = False
        if os.environ.get("AGENT_NO_STREAM"):
            config.stream = False
        if os.environ.get("AGENT_NO_PERMISSION"):
            config.enable_permission = False

        # 权限模式
        if permission_mode := os.environ.get("AGENT_PERMISSION_MODE"):
            config.permission_mode = permission_mode

        # Coordinator 模式
        if os.environ.get("AGENT_COORDINATOR"):
            config.coordinator_mode = True

        # 会话持久化
        if os.environ.get("AGENT_NO_SESSION"):
            config.enable_session = False
        if resume_id := os.environ.get("AGENT_RESUME_SESSION"):
            config.resume_session = resume_id

        # Hooks
        if os.environ.get("AGENT_NO_HOOKS"):
            config.enable_hooks = False

        return config

    @classmethod
    def from_args(cls, args: Any) -> "Config":
        """从命令行参数加载配置"""
        config = cls.from_env()

        # 覆盖配置
        if hasattr(args, "provider") and args.provider:
            config.provider = args.provider
        if hasattr(args, "model") and args.model:
            config.model = args.model
        if hasattr(args, "base_url") and args.base_url:
            config.base_url = args.base_url

        # 如果 provider 被命令行参数覆盖，需要重新根据新的 provider 选择 base_url
        # （因为 from_env 中 base_url 的选择依赖于 provider）
        if hasattr(args, "provider") and args.provider:
            if args.provider == "anthropic":
                # 如果 base_url 不是通过命令行显式指定的，重新从环境变量读取
                if not (hasattr(args, "base_url") and args.base_url):
                    config.base_url = os.environ.get("ANTHROPIC_BASE_URL")
            elif args.provider == "openai":
                if not (hasattr(args, "base_url") and args.base_url):
                    config.base_url = os.environ.get("OPENAI_BASE_URL")
        if hasattr(args, "cwd") and args.cwd:
            config.cwd = args.cwd
        if hasattr(args, "system") and args.system:
            config.system_prompt = args.system

        # 布尔标志
        if hasattr(args, "no_stream") and args.no_stream:
            config.stream = False
        if hasattr(args, "no_permission") and args.no_permission:
            config.enable_permission = False

        # 权限模式
        if hasattr(args, "permission_mode") and args.permission_mode:
            config.permission_mode = args.permission_mode

        # Coordinator 模式
        if hasattr(args, "coordinator") and args.coordinator:
            config.coordinator_mode = True

        # 会话持久化
        if hasattr(args, "no_session") and args.no_session:
            config.enable_session = False
        if hasattr(args, "resume") and args.resume:
            config.resume_session = args.resume
        if hasattr(args, "list_sessions") and args.list_sessions:
            config.list_sessions = True

        # 其他布尔标志
        if hasattr(args, "max_turns") and args.max_turns is not None:
            config.max_turns = args.max_turns
        if hasattr(args, "no_budget") and args.no_budget:
            config.enable_budget = False
        if hasattr(args, "no_compact") and args.no_compact:
            config.enable_compact = False
        if hasattr(args, "no_retry") and args.no_retry:
            config.enable_retry = False

        # Hooks
        if hasattr(args, "no_hooks") and args.no_hooks:
            config.enable_hooks = False

        return config

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "max_turns": self.max_turns,
            "enable_budget": self.enable_budget,
            "enable_compact": self.enable_compact,
            "enable_retry": self.enable_retry,
            "max_retries": self.max_retries,
            "stream": self.stream,
            "verbose": self.verbose,
            "cwd": self.cwd,
            "system_prompt": self.system_prompt,
            "enable_permission": self.enable_permission,
            "permission_mode": self.permission_mode,
            "coordinator_mode": self.coordinator_mode,
            "enable_session": self.enable_session,
            "resume_session": self.resume_session,
            "enable_hooks": self.enable_hooks,
        }


def get_default_model(provider: str) -> str:
    """获取默认模型名称"""
    defaults = {
        "anthropic": "claude-sonnet-4-20250514",
        "openai": "gpt-4o",
    }
    return defaults.get(provider, "claude-sonnet-4-20250514")


def load_config(args: Any | None = None) -> Config:
    """
    加载完整配置。

    优先级：命令行参数 > 环境变量 > 默认值

    Args:
        args: 命令行参数（argparse.Namespace）

    Returns:
        Config 实例
    """
    if args:
        return Config.from_args(args)
    return Config.from_env()
