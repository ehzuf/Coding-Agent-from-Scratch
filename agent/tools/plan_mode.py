"""
Plan Mode 工具 —— 让 Agent 先规划再执行

对应 reference 中的 tools/EnterPlanModeTool/ + tools/ExitPlanModeTool/

设计：
  - EnterPlanModeTool: Agent 主动进入规划模式，此时只能使用只读工具
  - ExitPlanModeTool:  Agent 提交方案并退出规划模式，恢复全部工具权限
  - Agent 通过 plan_mode 标志管理模式状态
  - 规划模式下写入类工具（write、edit、bash 非只读命令）被禁止

只读工具列表：
  - read, glob, grep, get_current_time, agent, send_message
  - bash（仅只读命令：ls, cat, head, tail, find, grep, wc, file, which, echo, pwd, git status/log/diff/show）
"""

from typing import Any
from agent.tools.base import Tool


# 只读工具白名单
READONLY_TOOLS = {"read", "glob", "grep", "get_current_time", "agent", "send_message"}

# bash 只读命令前缀（允许在 plan mode 下执行）
BASH_READONLY_PREFIXES = (
    "ls", "cat", "head", "tail", "find", "grep", "rg",
    "wc", "file", "which", "echo", "pwd", "tree", "du", "df",
    "git status", "git log", "git diff", "git show", "git branch",
    "git remote", "git tag",
    "python --version", "python3 --version", "node --version",
    "pip list", "pip show",
)


def is_tool_readonly(tool_name: str, tool_input: dict) -> bool:
    """
    判断工具调用是否为只读操作。

    Args:
        tool_name:  工具名
        tool_input: 工具输入参数

    Returns:
        True 表示只读，False 表示有写入副作用
    """
    if tool_name in READONLY_TOOLS:
        return True

    if tool_name == "bash":
        command = tool_input.get("command", "").strip()
        for prefix in BASH_READONLY_PREFIXES:
            if command == prefix or command.startswith(prefix + " "):
                return True
        return False

    # enter_plan_mode 和 exit_plan_mode 自身是特殊的
    if tool_name in ("enter_plan_mode", "exit_plan_mode"):
        return True

    return False


class EnterPlanModeTool(Tool):
    """
    进入规划模式。

    Agent 调用此工具后进入 Plan Mode：
      - 只能使用只读工具（read, glob, grep 等）
      - 写入类操作（write, edit, bash 写命令）被禁止
      - Agent 应专注于分析代码、理解需求、制定方案

    此工具不接受参数，返回确认消息。
    """

    @property
    def name(self) -> str:
        return "enter_plan_mode"

    @property
    def description(self) -> str:
        return (
            "进入规划模式，用于复杂任务的前期分析和方案设计。"
            "进入后只能使用只读工具（read, glob, grep 等），不能修改文件。"
            "完成规划后调用 exit_plan_mode 提交方案并恢复写入权限。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    def call(self, input: dict[str, Any]) -> str:
        # 实际的模式切换在 Agent._execute_tool() 中处理
        return (
            "已进入规划模式。\n\n"
            "当前限制：\n"
            "- 只能使用只读工具（read, glob, grep, bash 只读命令等）\n"
            "- 不能修改文件（write, edit 被禁止）\n"
            "- 不能执行有副作用的 bash 命令\n\n"
            "请分析代码、理解需求、制定方案。\n"
            "完成后调用 exit_plan_mode 提交方案。"
        )

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True


class ExitPlanModeTool(Tool):
    """
    退出规划模式并提交方案。

    Agent 调用此工具提交方案，退出 Plan Mode：
      - 恢复全部工具权限
      - 方案内容作为上下文保留在对话中
      - Agent 可以开始执行方案

    参数：
      - plan: 方案内容（markdown 格式）
    """

    @property
    def name(self) -> str:
        return "exit_plan_mode"

    @property
    def description(self) -> str:
        return (
            "退出规划模式并提交方案。"
            "提交后恢复全部工具权限（可以修改文件、执行命令），开始执行方案。"
            "plan 参数应包含完整的实施方案。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "实施方案（markdown 格式），包含分析结论和具体步骤",
                },
            },
            "required": ["plan"],
        }

    def call(self, input: dict[str, Any]) -> str:
        plan = input.get("plan", "")
        # 实际的模式切换在 Agent._execute_tool() 中处理
        return (
            "已退出规划模式，恢复全部工具权限。\n\n"
            f"## 方案\n\n{plan}\n\n"
            "现在可以开始执行上述方案。"
        )

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True
