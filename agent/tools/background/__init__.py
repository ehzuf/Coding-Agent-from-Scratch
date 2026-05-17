"""
后台 Bash 任务模块

五层解耦：
  - shell.py          : BackgroundShellCommand —— 后台进程托管 + 输出双写
  - registry.py       : TaskRegistry —— 全局任务注册表（id → task）
  - notifications.py  : NotificationQueue —— 完成通知队列（入队不触发）
  - sleep.py          : SleepTool —— Agent 主动等待，轮询队列 · 有通知立即唤醒
  - tools.py          : BashOutputTool / KillBashTool / ListBackgroundTasksTool
                        —— LLM 可调用的查询/终止/列表工具

设计要点见 TUTORIAL/25-background-bash.md。
"""

from .shell import BackgroundShellCommand
from .registry import TaskRegistry, background_registry
from .notifications import (
    NotificationQueue,
    QueuedNotification,
    notification_queue,
)
from .sleep import SleepTool, SLEEP_TOOL_NAME
from .tools import BashOutputTool, KillBashTool, ListBackgroundTasksTool

__all__ = [
    "BackgroundShellCommand",
    "TaskRegistry",
    "background_registry",
    "NotificationQueue",
    "QueuedNotification",
    "notification_queue",
    "SleepTool",
    "SLEEP_TOOL_NAME",
    "BashOutputTool",
    "KillBashTool",
    "ListBackgroundTasksTool",
]
