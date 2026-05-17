"""
通知队列：后台任务完成后只入队，不主动触发 LLM。

核心设计（参考 reference/utils/messageQueueManager.ts）：
  - 全局单例队列，task 完成时入队
  - 三档优先级 now/next/later，用户意图隐含 next，后台通知默认 later
  - drain() 由 Agent 主循环在"下一次 LLM 调用前"调用
  - 队列和 UI/输入解耦——任务完成不会打断用户输入

详见 TUTORIAL/25-background-bash.md"核心难点：完成后怎么通知 Agent"一节。
"""

import threading
from dataclasses import dataclass
from typing import Literal

Priority = Literal["now", "next", "later"]

_PRIORITY_ORDER: dict[str, int] = {"now": 0, "next": 1, "later": 2}


@dataclass
class QueuedNotification:
    """一条待投递的通知。value 通常是 XML 格式的任务完成块。"""

    value: str
    mode: str = "task-notification"
    priority: Priority = "later"
    agent_id: str | None = None


class NotificationQueue:
    """
    全局通知队列。

    - enqueue() 仅改数据，不做任何触发动作
    - drain() 取出并移除属于当前 agent 的通知，按优先级排序返回
    - 锁保护的原因：_pump 线程入队与主线程 drain 会并发访问 _queue
    """

    def __init__(self) -> None:
        self._queue: list[QueuedNotification] = []
        self._lock = threading.Lock()

    def enqueue(self, notif: QueuedNotification) -> None:
        with self._lock:
            self._queue.append(notif)

    def drain(
        self,
        agent_id: str | None = None,
        max_priority: Priority = "next",
    ) -> list[QueuedNotification]:
        """
        取出所有优先级 ≤ max_priority、属于 agent_id 的通知并从队列中移除。

        对齐 reference/query.ts 的 getCommandsByMaxPriority(sleepRan ? 'later' : 'next')：
          - 默认阈值是 'next'——搭车时不拉 later，避免打断用户当前话题
          - Agent 主动调了 SleepTool 时把门槛放宽到 'later'——把后台通知也捞上来

        Args:
            agent_id:     子 Agent 隔离；主 Agent 传 None 只拿主 Agent 的通知
            max_priority: 优先级上限（含）。被过滤掉的通知仍留在队列，等下一次机会

        Returns:
            按优先级排序的通知列表（now → next → later）
        """
        threshold = _PRIORITY_ORDER[max_priority]
        with self._lock:
            mine: list[QueuedNotification] = []
            rest: list[QueuedNotification] = []
            for n in self._queue:
                same_agent = n.agent_id == agent_id
                within = _PRIORITY_ORDER[n.priority] <= threshold
                if same_agent and within:
                    mine.append(n)
                else:
                    rest.append(n)
            self._queue = rest
        mine.sort(key=lambda n: _PRIORITY_ORDER[n.priority])
        return mine

    def peek_all(self) -> list[QueuedNotification]:
        """只读副本，用于调试/测试。"""
        with self._lock:
            return list(self._queue)

    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    def has_notifications_for(
        self,
        agent_id: str | None = None,
        max_priority: Priority = "later",
    ) -> bool:
        """
        是否存在属于 agent_id、优先级 ≤ max_priority 的通知。

        供 SleepTool 轮询使用——SleepTool 无视搭车阈值，只要有任何待投递通知
        就应该立即唤醒（对齐 reference channelNotification.ts 注释：
        "SleepTool polls hasCommandsInQueue() and wakes within 1s"）。
        """
        threshold = _PRIORITY_ORDER[max_priority]
        with self._lock:
            for n in self._queue:
                if n.agent_id == agent_id and _PRIORITY_ORDER[n.priority] <= threshold:
                    return True
        return False

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()


# 全局单例
notification_queue = NotificationQueue()
