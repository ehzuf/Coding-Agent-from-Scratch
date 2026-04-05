"""
Hooks 系统 —— 工具调用前后执行用户自定义脚本

对应 reference 中的 utils/hooks.ts + types/hooks.ts + schemas/hooks.ts

核心设计：
  - 三种 Hook 事件：PreToolUse、PostToolUse、UserPromptSubmit
  - Hook 配置从 ~/.coding-agent/settings.json 的 "hooks" 字段读取
  - Hook 是 shell 命令，通过环境变量接收上下文信息
  - PreToolUse 可以阻止工具执行（exit code 2 = block）
  - PostToolUse 的 stdout 作为 additional context 注入对话
  - Hook 命令超时默认 10 秒

Hook 事件：
  PreToolUse:
    - 触发时机：工具执行前（权限检查之后）
    - 环境变量：TOOL_NAME, TOOL_INPUT（JSON）, CWD
    - exit 0 = allow, exit 2 = block（stdout 作为 block reason）
    - stdout 如果是 JSON 且包含 "updatedInput"，可以修改工具输入
  PostToolUse:
    - 触发时机：工具执行后
    - 环境变量：TOOL_NAME, TOOL_INPUT（JSON）, TOOL_RESULT, CWD
    - stdout 作为 additional context 附加到消息
  UserPromptSubmit:
    - 触发时机：用户提交消息后、发送给 LLM 前
    - 环境变量：USER_PROMPT, CWD
    - stdout 作为 additional context 附加到消息

配置文件格式（~/.coding-agent/settings.json）：
  {
    "hooks": {
      "PreToolUse": [
        {
          "matcher": "bash",
          "hooks": [
            {"type": "command", "command": "echo 'about to run bash'", "timeout": 5}
          ]
        }
      ],
      "PostToolUse": [...],
      "UserPromptSubmit": [...]
    }
  }
"""

import json
import os
import subprocess
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Hook 事件类型
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit")

# 默认超时（秒）
DEFAULT_HOOK_TIMEOUT = 10


def _get_settings_path() -> Path:
    """获取全局设置文件路径。"""
    return Path.home() / ".coding-agent" / "settings.json"


def _get_project_settings_path(cwd: str) -> Path:
    """获取项目级设置文件路径。"""
    return Path(cwd) / ".coding-agent" / "settings.json"


@dataclass
class HookCommand:
    """单个 Hook 命令。"""
    type: str = "command"
    command: str = ""
    timeout: int = DEFAULT_HOOK_TIMEOUT

    @classmethod
    def from_dict(cls, data: dict) -> "HookCommand":
        return cls(
            type=data.get("type", "command"),
            command=data.get("command", ""),
            timeout=data.get("timeout", DEFAULT_HOOK_TIMEOUT),
        )


@dataclass
class HookMatcher:
    """Hook 匹配器：匹配工具名后执行 hooks 列表。"""
    matcher: str = ""  # 空字符串匹配所有
    hooks: list[HookCommand] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "HookMatcher":
        hooks = [HookCommand.from_dict(h) for h in data.get("hooks", [])]
        return cls(
            matcher=data.get("matcher", ""),
            hooks=hooks,
        )

    def matches(self, value: str) -> bool:
        """检查值是否匹配 matcher 模式。"""
        if not self.matcher:
            return True  # 空 matcher 匹配所有
        # 支持通配符匹配
        return fnmatch.fnmatch(value.lower(), self.matcher.lower())


@dataclass
class HookResult:
    """Hook 执行结果。"""
    outcome: str = "success"  # success, blocked, error, timeout
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    block_reason: str = ""
    additional_context: str = ""
    updated_input: dict | None = None

    @property
    def is_blocked(self) -> bool:
        return self.outcome == "blocked"


@dataclass
class AggregatedHookResult:
    """
    多个 Hook 的聚合结果。

    规则：
      - 任何一个 Hook block → 整体 blocked
      - 所有 additional_context 合并
      - 最后一个 updated_input 生效
    """
    is_blocked: bool = False
    block_reason: str = ""
    additional_contexts: list[str] = field(default_factory=list)
    updated_input: dict | None = None
    results: list[HookResult] = field(default_factory=list)


def _run_hook_command(
    hook: HookCommand,
    env: dict[str, str],
    cwd: str,
) -> HookResult:
    """
    执行单个 Hook 命令。

    Hook 通过环境变量接收上下文信息，通过 exit code 和 stdout 返回结果。

    Args:
        hook:  HookCommand 实例
        env:   传递给子进程的环境变量
        cwd:   工作目录

    Returns:
        HookResult
    """
    if not hook.command:
        return HookResult(outcome="success")

    # 合并系统环境变量和 Hook 环境变量
    full_env = {**os.environ, **env}

    try:
        result = subprocess.run(
            hook.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=hook.timeout,
            cwd=cwd,
            env=full_env,
        )
    except subprocess.TimeoutExpired:
        return HookResult(
            outcome="timeout",
            stderr=f"Hook 超时（{hook.timeout}s）: {hook.command}",
            exit_code=-1,
        )
    except Exception as e:
        return HookResult(
            outcome="error",
            stderr=str(e),
            exit_code=-1,
        )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    # exit code 2 = block（参考 Claude Code 的约定）
    if result.returncode == 2:
        return HookResult(
            outcome="blocked",
            stdout=stdout,
            stderr=stderr,
            exit_code=2,
            block_reason=stdout or stderr or f"Blocked by hook: {hook.command}",
        )

    # 尝试解析 JSON 输出（支持 updatedInput）
    updated_input = None
    additional_context = ""
    if stdout.startswith("{"):
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                updated_input = parsed.get("updatedInput")
                additional_context = parsed.get("additionalContext", "")
        except json.JSONDecodeError:
            # 非 JSON 输出，作为 additional context
            additional_context = stdout
    else:
        additional_context = stdout

    # 非零退出码（非 2）视为非阻塞错误
    if result.returncode != 0:
        return HookResult(
            outcome="error",
            stdout=stdout,
            stderr=stderr,
            exit_code=result.returncode,
            additional_context=stderr or stdout,
        )

    return HookResult(
        outcome="success",
        stdout=stdout,
        stderr=stderr,
        exit_code=0,
        additional_context=additional_context,
        updated_input=updated_input,
    )


class HookManager:
    """
    Hooks 管理器。

    负责加载 Hook 配置、匹配并执行 Hook。

    用法：
        hm = HookManager(cwd="/path/to/project")

        # 工具执行前
        result = hm.run_pre_tool_use("bash", {"command": "ls"})
        if result.is_blocked:
            return f"被 Hook 阻止: {result.block_reason}"

        # 工具执行后
        result = hm.run_post_tool_use("bash", {"command": "ls"}, "file1\\nfile2")

        # 用户提交消息
        result = hm.run_user_prompt_submit("你好")
    """

    def __init__(self, cwd: str = "."):
        self.cwd = os.path.abspath(cwd)
        self._hooks_config: dict[str, list[HookMatcher]] = {}
        self._load_config()

    def _load_config(self) -> None:
        """从设置文件加载 Hook 配置。"""
        # 全局配置
        global_hooks = self._load_hooks_from_file(_get_settings_path())

        # 项目配置（优先级更高，合并到全局配置之后）
        project_hooks = self._load_hooks_from_file(
            _get_project_settings_path(self.cwd)
        )

        # 合并：项目配置追加到全局配置后面
        self._hooks_config = {}
        for event in HOOK_EVENTS:
            matchers = []
            if event in global_hooks:
                matchers.extend(global_hooks[event])
            if event in project_hooks:
                matchers.extend(project_hooks[event])
            if matchers:
                self._hooks_config[event] = matchers

    def _load_hooks_from_file(self, path: Path) -> dict[str, list[HookMatcher]]:
        """从单个设置文件加载 hooks 配置。"""
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            hooks_data = data.get("hooks", {})
            result = {}
            for event, matchers_data in hooks_data.items():
                if event not in HOOK_EVENTS:
                    continue
                if not isinstance(matchers_data, list):
                    continue
                result[event] = [HookMatcher.from_dict(m) for m in matchers_data]
            return result
        except (json.JSONDecodeError, OSError):
            return {}

    def _find_matching_hooks(
        self,
        event: str,
        match_value: str,
    ) -> list[HookCommand]:
        """
        找到匹配的 Hook 命令列表。

        Args:
            event:       Hook 事件名
            match_value: 用于匹配的值（如工具名、空字符串）

        Returns:
            匹配的 HookCommand 列表
        """
        matchers = self._hooks_config.get(event, [])
        matched_hooks = []
        for matcher in matchers:
            if matcher.matches(match_value):
                matched_hooks.extend(matcher.hooks)
        return matched_hooks

    def _run_hooks(
        self,
        hooks: list[HookCommand],
        env: dict[str, str],
    ) -> AggregatedHookResult:
        """
        执行一组 Hook 并聚合结果。

        规则：
          - 按顺序执行
          - 任何一个 block → 停止后续 Hook，整体返回 blocked
          - additional_context 累积
          - updated_input 取最后一个非 None 值

        Returns:
            AggregatedHookResult
        """
        agg = AggregatedHookResult()

        for hook in hooks:
            result = _run_hook_command(hook, env, self.cwd)
            agg.results.append(result)

            if result.is_blocked:
                agg.is_blocked = True
                agg.block_reason = result.block_reason
                break  # 被阻止，不再执行后续 Hook

            if result.additional_context:
                agg.additional_contexts.append(result.additional_context)

            if result.updated_input is not None:
                agg.updated_input = result.updated_input

        return agg

    def run_pre_tool_use(
        self,
        tool_name: str,
        tool_input: dict,
    ) -> AggregatedHookResult:
        """
        执行 PreToolUse Hook。

        Args:
            tool_name:  工具名
            tool_input: 工具输入参数

        Returns:
            AggregatedHookResult（检查 .is_blocked 决定是否阻止工具执行）
        """
        hooks = self._find_matching_hooks("PreToolUse", tool_name)
        if not hooks:
            return AggregatedHookResult()

        env = {
            "HOOK_EVENT": "PreToolUse",
            "TOOL_NAME": tool_name,
            "TOOL_INPUT": json.dumps(tool_input, ensure_ascii=False),
            "CWD": self.cwd,
        }
        return self._run_hooks(hooks, env)

    def run_post_tool_use(
        self,
        tool_name: str,
        tool_input: dict,
        tool_result: str,
    ) -> AggregatedHookResult:
        """
        执行 PostToolUse Hook。

        Args:
            tool_name:   工具名
            tool_input:  工具输入参数
            tool_result: 工具执行结果

        Returns:
            AggregatedHookResult（.additional_contexts 可注入对话）
        """
        hooks = self._find_matching_hooks("PostToolUse", tool_name)
        if not hooks:
            return AggregatedHookResult()

        env = {
            "HOOK_EVENT": "PostToolUse",
            "TOOL_NAME": tool_name,
            "TOOL_INPUT": json.dumps(tool_input, ensure_ascii=False),
            "TOOL_RESULT": tool_result[:5000],  # 截断过长的结果
            "CWD": self.cwd,
        }
        return self._run_hooks(hooks, env)

    def run_user_prompt_submit(
        self,
        prompt: str,
    ) -> AggregatedHookResult:
        """
        执行 UserPromptSubmit Hook。

        Args:
            prompt: 用户输入的消息

        Returns:
            AggregatedHookResult（.additional_contexts 可注入对话）
        """
        hooks = self._find_matching_hooks("UserPromptSubmit", "")
        if not hooks:
            return AggregatedHookResult()

        env = {
            "HOOK_EVENT": "UserPromptSubmit",
            "USER_PROMPT": prompt,
            "CWD": self.cwd,
        }
        return self._run_hooks(hooks, env)

    def has_hooks(self, event: str) -> bool:
        """检查指定事件是否有配置的 Hook。"""
        return event in self._hooks_config and len(self._hooks_config[event]) > 0

    def get_hooks_summary(self) -> str:
        """获取 Hook 配置摘要。"""
        if not self._hooks_config:
            return "没有配置 Hooks。"

        lines = []
        for event in HOOK_EVENTS:
            matchers = self._hooks_config.get(event, [])
            if not matchers:
                continue
            total_hooks = sum(len(m.hooks) for m in matchers)
            matcher_descs = []
            for m in matchers:
                pattern = m.matcher or "*"
                cmds = [h.command for h in m.hooks]
                matcher_descs.append(f"    {pattern}: {cmds}")
            lines.append(f"  {event} ({total_hooks} 个):")
            lines.extend(matcher_descs)

        return "Hooks 配置:\n" + "\n".join(lines)

    def reload(self) -> None:
        """重新加载 Hook 配置。"""
        self._load_config()
