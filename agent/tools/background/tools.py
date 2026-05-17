"""
后台任务相关的 LLM 可调用工具：
  - bash_output          : 读取指定 task 的最近输出
  - kill_bash            : 终止指定 task
  - list_background_tasks: 列出当前所有后台任务（含状态）
"""

from typing import Any

from agent.tools.base import Tool

from .registry import background_registry


class BashOutputTool(Tool):
    """读取后台任务的最近输出。"""

    @property
    def name(self) -> str:
        return "bash_output"

    @property
    def description(self) -> str:
        return (
            "读取一个后台 bash 任务的最近输出（最多 1000 行环形缓冲）。\n"
            "通常在 bash(run_in_background=true) 启动任务后使用。\n"
            "返回 task 当前状态、退出码（如已结束）和最近输出。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "后台任务 id，格式形如 bg-xxxxxxxx",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "最多返回的行数（默认 200，上限 1000）",
                },
            },
            "required": ["task_id"],
        }

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        # 只读，随便并发
        return True

    def call(self, input: dict[str, Any]) -> str:
        task_id = input.get("task_id", "")
        max_lines = input.get("max_lines", 200)
        try:
            max_lines = int(max_lines)
        except (TypeError, ValueError):
            max_lines = 200
        max_lines = max(1, min(max_lines, 1000))

        task = background_registry.get(task_id)
        if task is None:
            return f"错误：找不到后台任务 {task_id}"

        tail = task.read_tail(max_lines=max_lines)
        parts = [
            f"task_id: {task.id}",
            f"command: {task.command}",
            f"status: {task.status}",
        ]
        if task.exit_code is not None:
            parts.append(f"exit_code: {task.exit_code}")
        parts.append(f"output_file: {task.output_file}")
        parts.append(f"--- 最近输出（最多 {max_lines} 行）---")
        parts.append(tail if tail else "(暂无输出)")
        return "\n".join(parts)


class KillBashTool(Tool):
    """终止一个后台任务。"""

    @property
    def name(self) -> str:
        return "kill_bash"

    @property
    def description(self) -> str:
        return (
            "终止一个后台 bash 任务。任务进程会被 SIGTERM，状态置为 killed。\n"
            "若任务已结束则返回提示，不影响其他任务。"
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "要终止的后台任务 id",
                },
            },
            "required": ["task_id"],
        }

    def call(self, input: dict[str, Any]) -> str:
        task_id = input.get("task_id", "")
        task = background_registry.get(task_id)
        if task is None:
            return f"错误：找不到后台任务 {task_id}"
        ok = task.kill()
        if ok:
            return f"任务 {task.id} 已发送终止信号"
        return f"任务 {task.id} 已经是 {task.status} 状态，无需终止"


class ListBackgroundTasksTool(Tool):
    """列出当前所有后台任务。"""

    @property
    def name(self) -> str:
        return "list_background_tasks"

    @property
    def description(self) -> str:
        return "列出当前所有后台任务（含已完成/失败/被杀），显示 id、command、status、exit_code。"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def call(self, input: dict[str, Any]) -> str:
        tasks = background_registry.list_all()
        if not tasks:
            return "(当前没有后台任务)"
        lines = [f"共 {len(tasks)} 个后台任务："]
        for t in tasks:
            snap = t.snapshot()
            rc = snap["exit_code"] if snap["exit_code"] is not None else "-"
            cmd = snap["command"]
            if len(cmd) > 60:
                cmd = cmd[:60] + "..."
            lines.append(
                f"  [{snap['status']:9s}] {snap['id']}  exit={rc}  cmd={cmd}"
            )
        return "\n".join(lines)
