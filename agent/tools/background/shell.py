"""
BackgroundShellCommand —— 后台运行的 shell 进程。

核心职责：
  1. 用 subprocess.Popen 启动进程，不阻塞调用方
  2. 后台线程 _pump 把 stdout/stderr 持续搬运到：
       - 内存环形缓冲（1000 行，O(1) 读取最近输出）
       - 磁盘文件（完整历史，防止内存爆炸）
  3. 进程结束时自动格式化 <task-notification> XML 并入队通知
  4. 支持 kill() 终止

状态机：running → completed/failed/killed
所有状态转换在 _lock 内完成，避免 _pump 线程和 kill() 并发竞态。

参考 reference/utils/ShellCommand.ts 的简化版。
"""

import subprocess
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Literal

from .notifications import (
    QueuedNotification,
    notification_queue,
)

TaskStatus = Literal["running", "completed", "failed", "killed"]


_TASK_NOTIFICATION_TEMPLATE = (
    "<task-notification>\n"
    "<task-id>{id}</task-id>\n"
    "<task-type>shell</task-type>\n"
    "<command>{cmd}</command>\n"
    "<output-file>{path}</output-file>\n"
    "<exit-code>{rc}</exit-code>\n"
    "<status>{status}</status>\n"
    "<summary>{summary}</summary>\n"
    "</task-notification>"
)

_STATUS_TEXT = {
    "completed": 'Command "{cmd}" completed successfully (exit 0)',
    "failed": 'Command "{cmd}" failed with exit code {rc}',
    "killed": 'Command "{cmd}" was killed',
}


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class BackgroundShellCommand:
    """
    一个后台 shell 进程，带输出双写 + 完成自动通知。

    典型使用：
        task = BackgroundShellCommand("sleep 2 && echo done")
        # ... 主流程继续 ...
        task.wait_done(timeout=5)       # 测试用
        print(task.read_tail())         # 读最近输出
        print(task.status)              # completed
    """

    _BUFFER_LINES = 1000
    _OUTPUT_DIR = Path.home() / ".coding-agent" / "bg_tasks"

    def __init__(
        self,
        command: str,
        cwd: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.id: str = f"bg-{uuid.uuid4().hex[:8]}"
        self.command: str = command
        self.cwd: str | None = cwd
        self.agent_id: str | None = agent_id
        self.status: TaskStatus = "running"
        self.exit_code: int | None = None

        # 磁盘文件：完整历史
        self._OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.output_file: Path = self._OUTPUT_DIR / f"{self.id}.log"

        # 内存环形缓冲：最近 N 行
        self._tail: deque[str] = deque(maxlen=self._BUFFER_LINES)

        # 状态锁 + 完成事件
        self._lock = threading.Lock()
        self._done = threading.Event()

        # 启动进程
        self._fh = self.output_file.open("w", buffering=1)  # 行缓冲
        self._proc = subprocess.Popen(
            ["bash", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd,
        )

        # 后台线程持续搬运输出
        self._reader = threading.Thread(
            target=self._pump, name=f"bg-pump-{self.id}", daemon=True
        )
        self._reader.start()

    # ------------------------------------------------------------------
    # 内部：输出搬运 + 完成回调
    # ------------------------------------------------------------------

    def _pump(self) -> None:
        """把子进程 stdout 持续写入环形缓冲和文件，结束后发完成通知。"""
        assert self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                with self._lock:
                    self._tail.append(line)
                    self._fh.write(line)
                    self._fh.flush()
        except Exception:
            # 管道异常断开：继续走完流程
            pass

        rc = self._proc.wait()

        with self._lock:
            # 如果已经被 kill() 标过，不覆盖
            if self.status == "running":
                self.status = "completed" if rc == 0 else "failed"
            self.exit_code = rc
            try:
                self._fh.close()
            except Exception:
                pass

        # 触发完成事件（供 wait_done / 测试使用）
        self._done.set()

        # 入队一条任务完成通知
        self._enqueue_finish_notification()

    def _enqueue_finish_notification(self) -> None:
        cmd_short = self.command if len(self.command) <= 200 else self.command[:200] + "..."
        rc = self.exit_code if self.exit_code is not None else -1
        summary_fmt = _STATUS_TEXT.get(self.status, f'Command "{{cmd}}" ended with status {self.status}')
        summary = summary_fmt.format(cmd=cmd_short, rc=rc)

        xml = _TASK_NOTIFICATION_TEMPLATE.format(
            id=self.id,
            cmd=_xml_escape(cmd_short),
            path=_xml_escape(str(self.output_file)),
            rc=rc,
            status=self.status,
            summary=_xml_escape(summary),
        )

        notification_queue.enqueue(
            QueuedNotification(
                value=xml,
                mode="task-notification",
                priority="later",
                agent_id=self.agent_id,
            )
        )

    # ------------------------------------------------------------------
    # 公共：读取 / 终止 / 等待
    # ------------------------------------------------------------------

    def read_tail(self, max_lines: int | None = None) -> str:
        """读取最近若干行（默认全部 1000 行环形缓冲）。"""
        with self._lock:
            lines = list(self._tail)
        if max_lines is not None:
            lines = lines[-max_lines:]
        return "".join(lines)

    def kill(self) -> bool:
        """
        终止进程。

        Returns:
            True 表示本次调用确实发出了 SIGTERM；False 表示任务已经结束。
        """
        with self._lock:
            if self.status != "running":
                return False
            # 标记先行，避免 _pump 把状态改成 completed/failed
            self.status = "killed"
        try:
            self._proc.terminate()
        except Exception:
            pass
        return True

    def wait_done(self, timeout: float | None = None) -> bool:
        """
        阻塞等待任务结束（主要供测试使用）。

        Returns:
            True 表示已完成；False 表示超时仍在运行。
        """
        return self._done.wait(timeout=timeout)

    def snapshot(self) -> dict:
        """供 list/introspect 用的只读快照。"""
        with self._lock:
            return {
                "id": self.id,
                "command": self.command,
                "status": self.status,
                "exit_code": self.exit_code,
                "output_file": str(self.output_file),
            }
