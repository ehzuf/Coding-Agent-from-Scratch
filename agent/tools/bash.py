"""
Bash 工具：执行 shell 命令

对应 reference 中的 tools/BashTool/

核心功能：
  - 执行 shell 命令，捕获 stdout/stderr
  - 支持超时设置
  - 支持指定工作目录
  - 返回格式化的结果（包含退出码、输出等）

安全考虑：
  - 命令在 shell 中执行，需要注意命令注入风险
  - 后续会添加权限系统来控制哪些命令可以执行
"""

import subprocess
import os
import shlex
from typing import Any

from .base import Tool


class BashTool(Tool):
    """执行 shell 命令的工具。"""

    # 已知的只读命令前缀（不修改文件系统或外部状态）
    _READONLY_COMMANDS = frozenset([
        "ls", "cat", "head", "tail", "wc", "find", "which", "whoami",
        "pwd", "echo", "date", "env", "printenv", "uname", "hostname",
        "file", "stat", "du", "df", "free", "uptime", "id",
        "grep", "egrep", "fgrep", "rg", "ag", "ack",
        "diff", "cmp", "md5sum", "sha256sum", "shasum",
        "tree", "realpath", "dirname", "basename",
        "git status", "git log", "git diff", "git show", "git branch",
        "git remote", "git tag", "git rev-parse", "git ls-files",
    ])

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return """执行 shell 命令。

命令会在 shell 中执行（bash -c），支持管道、重定向等 shell 特性。
默认超时为 120 秒，工作目录为当前目录。

返回结果包含：
- 退出码（0 表示成功）
- 标准输出内容
- 标准错误内容

对于长时间运行的命令（如 npm run dev / pytest --watch / python train.py），
传 run_in_background=true 立即托管到后台并返回 task_id，
之后用 bash_output(task_id) 查看实时输出，用 kill_bash(task_id) 终止。
任务完成后会自动以 <task-notification> 通知形式出现在下一轮对话上下文中。

注意：此工具功能强大，后续会添加权限控制。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "timeout": {
                    "type": "number",
                    "description": "超时时间（毫秒），默认 120000（仅同步模式生效）",
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录，默认为当前目录",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "设为 true 将命令托管到后台并立即返回 task_id，"
                        "不阻塞当前对话。用于 npm run dev / pytest --watch 等长任务。"
                    ),
                },
            },
            "required": ["command"],
        }

    def _is_readonly(self, command: str) -> bool:
        """
        判断命令是否只读（不修改文件系统或外部状态）。

        策略：提取命令的第一个 token（可能包含路径前缀），
        与已知只读命令列表比较。对于管道命令，只检查第一段。
        """
        # 去掉前导空格和环境变量赋值 (如 FOO=bar cmd)
        cmd = command.strip()
        if not cmd:
            return False

        # 取管道/分号/&&之前的第一段命令
        for sep in ("|", "&&", "||", ";"):
            cmd = cmd.split(sep)[0].strip()

        # 检查 git 子命令（如 "git status"）
        for readonly_cmd in self._READONLY_COMMANDS:
            if " " in readonly_cmd and cmd.startswith(readonly_cmd):
                return True

        # 提取第一个 token 作为命令名
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return False

        if not tokens:
            return False

        # 取命令名（去掉路径前缀）
        cmd_name = os.path.basename(tokens[0])

        return cmd_name in self._READONLY_COMMANDS

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        command = input.get("command", "")
        return self._is_readonly(command)

    def _start_background(self, command: str, cwd: str | None) -> str:
        """启动后台任务，立即返回 task_id，不等进程结束。"""
        # 延迟导入避免顶层循环依赖
        from .background import background_registry

        task = background_registry.spawn(command, cwd=cwd)
        return (
            f"后台任务已启动\n"
            f"task_id: {task.id}\n"
            f"command: {command}\n"
            f"output_file: {task.output_file}\n"
            f"提示：用 bash_output(task_id) 查看实时输出，用 kill_bash(task_id) 终止。\n"
            f"任务完成后将以 <task-notification> 通知形式出现在后续对话上下文中。"
        )

    def call(self, input: dict[str, Any]) -> str:
        command = input.get("command", "")
        timeout = input.get("timeout", 120000)
        cwd = input.get("cwd")
        run_in_background = bool(input.get("run_in_background", False))

        if not command:
            return "错误：command 参数不能为空"

        # 验证工作目录
        if cwd and not os.path.isdir(cwd):
            return f"错误：工作目录不存在: {cwd}"

        # 显式后台分支：立即托管返回 task_id
        if run_in_background:
            return self._start_background(command, cwd)

        try:
            result = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout / 1000,  # 转换为秒
                cwd=cwd,
            )

            # 构建输出
            output_parts = []

            if result.stdout:
                output_parts.append(f"stdout:\n{result.stdout}")

            if result.stderr:
                output_parts.append(f"stderr:\n{result.stderr}")

            output_parts.append(f"exit_code: {result.returncode}")

            return "\n\n".join(output_parts)

        except subprocess.TimeoutExpired:
            return f"错误：命令执行超时（{timeout}ms）"
        except Exception as e:
            return f"错误：执行命令失败 - {e}"
