"""
TaskRegistry —— 全局后台任务注册表。

提供两件事：
  1. spawn()：启动新后台任务并登记
  2. get() / list_running() / list_all()：按 id 查、按状态查

所有操作线程安全。
"""

import threading

from .shell import BackgroundShellCommand


class TaskRegistry:
    """线程安全的任务表：id → BackgroundShellCommand。"""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundShellCommand] = {}
        self._lock = threading.Lock()

    def spawn(
        self,
        command: str,
        cwd: str | None = None,
        agent_id: str | None = None,
    ) -> BackgroundShellCommand:
        task = BackgroundShellCommand(command, cwd=cwd, agent_id=agent_id)
        with self._lock:
            self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> BackgroundShellCommand | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_running(self) -> list[BackgroundShellCommand]:
        with self._lock:
            return [t for t in self._tasks.values() if t.status == "running"]

    def list_all(self) -> list[BackgroundShellCommand]:
        with self._lock:
            return list(self._tasks.values())

    def clear(self) -> None:
        """清空注册表（测试用；不会 kill 仍在运行的进程）。"""
        with self._lock:
            self._tasks.clear()


# 全局单例，BashTool._start_background 与 BashOutputTool/KillBashTool 共享
background_registry = TaskRegistry()
