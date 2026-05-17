"""
SleepTool —— Agent 主动等待的工具

对齐 reference/tools/SleepTool/prompt.ts 的设计：
  - Agent 在"没事可做/在等什么"时主动调用
  - 内部轮询 notification_queue，每秒检查一次
  - 一旦有待投递通知（任何优先级）就立即返回——对应
    reference/services/mcp/channelNotification.ts 的注释：
    "SleepTool polls hasCommandsInQueue() and wakes within 1s"
  - 到达 duration 上限也返回，避免 Agent 永久挂起

与 Claude Code 的区别：
  1. Claude Code 的 SleepTool 只在 proactive / KAIROS 模式下暴露；这里为
     了教学演示，直接注册给所有场景，但 prompt 里明确鼓励"只在确实在等
     异步结果时用"。
  2. 这里不支持用户按键打断，因为本项目没有 UI 输入流；但支持 duration
     上限，保证即使没有任何通知也会退出。

教程参见 TUTORIAL/25-background-bash.md 的《还有一半：SleepTool 主动等待》。
"""

import time
from typing import Any

from ..base import Tool
from .notifications import notification_queue


# 暴露给 agent.py 判断"上一轮是否调用了 SleepTool"，对应 reference 的 SLEEP_TOOL_NAME
SLEEP_TOOL_NAME = "sleep"


class SleepTool(Tool):
    """
    让 Agent 主动"睡一小会儿"，等待后台任务完成通知。

    典型用法（由 LLM 自行决定）：
      用户：帮我跑一个耗时脚本，跑完告诉我
      → Agent 调 bash(run_in_background=True) 启动后台任务
      → Agent 回一句 "已经启动"
      → Agent 调 sleep(max_seconds=30) 挂起等待通知
      → 后台任务完成入队 → SleepTool 1s 内醒来
      → 下一轮 agent 主循环把通知 drain 进 messages
      → Agent 主动向用户播报 "任务已完成"
    """

    @property
    def name(self) -> str:
        return SLEEP_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "Wait for a short period, returning immediately when any background "
            "task notification arrives.\n\n"
            "Use this when you have nothing else to do but are waiting for an "
            "asynchronous result (e.g. a long-running bash command you started "
            "with run_in_background=True). Polls for pending notifications every "
            "poll_interval seconds and wakes within that interval once a "
            "notification is enqueued.\n\n"
            "Do NOT use this as a generic delay. Prefer this over `bash(sleep ...)` — "
            "it doesn't hold a shell process and can be interrupted by incoming "
            "notifications. If you don't have a pending async result to wait on, "
            "just end your turn instead.\n\n"
            "Each wake-up triggers a new LLM turn, so balance max_seconds against "
            "cost. Reasonable default: 5–30 seconds."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "max_seconds": {
                    "type": "number",
                    "description": (
                        "Upper bound on wait duration in seconds. The tool returns "
                        "earlier if a background notification arrives. Range 1–300."
                    ),
                    "minimum": 1,
                    "maximum": 300,
                    "default": 10,
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short human-readable reason for waiting, e.g. "
                        "'waiting for bg task <id> to finish'. Helps debugging."
                    ),
                },
                "poll_interval": {
                    "type": "number",
                    "description": (
                        "How often (seconds) to check the notification queue. "
                        "Default 1.0. Lower = snappier wake but more CPU."
                    ),
                    "minimum": 0.05,
                    "maximum": 5,
                    "default": 1.0,
                },
            },
            "required": [],
        }

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        # sleep 本身没有副作用，但也没有理由并发——保持默认 False
        return False

    def call(self, input: dict[str, Any]) -> str:
        max_seconds = float(input.get("max_seconds", 10))
        poll_interval = float(input.get("poll_interval", 1.0))
        reason = str(input.get("reason") or "").strip()

        # 参数兜底
        max_seconds = max(0.0, min(max_seconds, 300.0))
        poll_interval = max(0.05, min(poll_interval, 5.0))

        # 边界：如果入队时就已经有通知，立即返回（不要浪费一秒）
        agent_id = getattr(self, "_agent_id", None)
        if notification_queue.has_notifications_for(agent_id=agent_id):
            return self._format_result(
                slept=0.0,
                woke_by="notification",
                reason=reason,
                max_seconds=max_seconds,
            )

        start = time.monotonic()
        deadline = start + max_seconds
        woke_by = "timeout"

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # 本轮 sleep 步长：min(剩余, poll_interval)
            step = min(poll_interval, remaining)
            time.sleep(step)
            if notification_queue.has_notifications_for(agent_id=agent_id):
                woke_by = "notification"
                break

        slept = time.monotonic() - start
        return self._format_result(
            slept=slept,
            woke_by=woke_by,
            reason=reason,
            max_seconds=max_seconds,
        )

    @staticmethod
    def _format_result(
        slept: float,
        woke_by: str,
        reason: str,
        max_seconds: float,
    ) -> str:
        lines = [
            f"slept: {slept:.2f}s (max={max_seconds:.1f}s)",
            f"woke_by: {woke_by}",
        ]
        if reason:
            lines.append(f"reason: {reason}")
        if woke_by == "notification":
            lines.append(
                "note: a background notification is pending and will be "
                "visible to you on the next turn."
            )
        else:
            lines.append(
                "note: no notification arrived before the timeout. "
                "End the turn (or sleep again) as appropriate."
            )
        return "\n".join(lines)
