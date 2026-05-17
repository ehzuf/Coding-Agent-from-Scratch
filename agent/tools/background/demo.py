"""
Background Bash 端到端 Demo
============================

不依赖真实 LLM，通过 FakeLLM 模拟 Agent 的对话轮次，验证教程
TUTORIAL/25-background-bash.md 描述的**两层通知机制**：

场景 A：搭车不打断（priority=later 默认被过滤）
  1. Agent 调 bash(run_in_background=True) 启动后台任务
  2. 任务在后台完成，入队一条 later 级 <task-notification>
  3. 用户再发消息——drain 阈值是 "next"，later 通知被过滤
  4. 结论：用户当前话题不会被后台任务打断

场景 B：Agent 主动 sleep，通知唤醒
  1. Agent 调 bash(run_in_background=True) 启动后台任务
  2. Agent 调 sleep(max_seconds=10) 主动等待异步结果
  3. 后台任务完成入队 → SleepTool 1s 内轮询到 → 立即返回
  4. 下一轮 drain：因为上一轮跑了 SleepTool，阈值升级到 "later"
  5. LLM 看到通知，主动回复用户

运行:
    python -m agent.tools.background.demo
"""

import time
from typing import Any

from agent.llm.base import BaseLLM, LLMResponse
from agent.tools import get_tools
from agent.tools.background import (
    background_registry,
    notification_queue,
)
from agent.agent import Agent


# ---------------------------------------------------------------------------
# FakeLLM：按脚本回答，避免真实 API 调用
# ---------------------------------------------------------------------------

class FakeLLM(BaseLLM):
    """
    按预设脚本响应的假 LLM。每次 chat() 弹出脚本中的下一条动作。

    脚本项格式：
      - {"text": "...", "tool_use": {"name": "...", "input": {...}}}
      - {"text": "..."}            # 纯文本结束本轮
    """

    def __init__(self, script: list[dict]):
        self.model = "fake-llm"
        self.script = list(script)
        self.turn_idx = 0
        self.last_messages_snapshot: list[dict] = []

    def chat(
        self,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        **_: Any,
    ) -> LLMResponse:
        self.last_messages_snapshot = [dict(m) for m in messages]

        if self.turn_idx >= len(self.script):
            return LLMResponse(
                content=[{"type": "text", "text": "(fake-llm: 脚本已结束)"}],
                input_tokens=0, output_tokens=0,
                model=self.model, stop_reason="end_turn",
            )

        step = self.script[self.turn_idx]
        self.turn_idx += 1

        content: list[dict] = []
        if step.get("text"):
            content.append({"type": "text", "text": step["text"]})

        tu = step.get("tool_use")
        if tu:
            content.append({
                "type": "tool_use",
                "id": f"toolu_{self.turn_idx}",
                "name": tu["name"],
                "input": tu["input"],
            })
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"

        return LLMResponse(
            content=content,
            input_tokens=0, output_tokens=0,
            model=self.model, stop_reason=stop_reason,
        )

    def stream(self, *args, **kwargs):
        raise NotImplementedError("FakeLLM 不支持 stream()")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def show_queue_state(label: str) -> None:
    size = notification_queue.size()
    print(f"[notification_queue @ {label}] size={size}")
    for i, n in enumerate(notification_queue.peek_all()):
        first_line = n.value.splitlines()[0] if n.value else ""
        print(f"  [{i}] priority={n.priority} mode={n.mode} head={first_line!r}")


def reset_global_state() -> None:
    notification_queue.clear()
    background_registry.clear()


def build_agent(fake: FakeLLM) -> Agent:
    return Agent(
        llm=fake,
        tools=get_tools(),
        enable_budget=False,
        enable_compact=False,
        enable_retry=False,
        enable_permission=False,
        max_turns=10,
        _enable_agent_tool=False,
    )


def last_llm_saw_notification(fake: FakeLLM) -> tuple[bool, str]:
    """检查 fake 最后一次 chat 的 messages 快照中是否含 [background-task-updates]。"""
    for m in fake.last_messages_snapshot:
        content = m.get("content", "")
        if isinstance(content, str) and "[background-task-updates]" in content:
            return True, content
    return False, ""


# ---------------------------------------------------------------------------
# 场景 A：搭车不打断
# ---------------------------------------------------------------------------

def demo_scenario_a() -> None:
    section("场景 A：搭车 drain 默认阈值 = 'next'，后台通知被过滤保留")
    reset_global_state()

    # 脚本：
    #   轮 1 -> bash run_in_background，end_turn
    #   轮 2 -> 纯文本（用户继续聊，drain 时应该过滤掉 later 通知）
    script = [
        {
            "text": "好的，我把它扔到后台跑。",
            "tool_use": {
                "name": "bash",
                "input": {
                    "command": "sleep 1 && echo DONE_A",
                    "run_in_background": True,
                },
            },
        },
        {"text": "任务启动了，继续说吧。"},
        {"text": "好呀，我们聊点别的。"},
    ]
    fake = FakeLLM(script=script)
    agent = build_agent(fake)

    # 轮 1
    show_queue_state("before round 1")
    resp = agent.chat("请用后台方式跑一个 sleep 1 && echo DONE_A")
    print(f"[assistant] {resp.text}")

    tasks = background_registry.list_all()
    assert len(tasks) == 1
    task = tasks[0]

    # 等任务结束
    print(f"[wait] task {task.id} ...")
    assert task.wait_done(timeout=5), "任务应该在 5s 内完成"
    time.sleep(0.1)
    show_queue_state("after task finished (later 级通知已入队)")
    assert notification_queue.size() == 1
    assert notification_queue.peek_all()[0].priority == "later"

    # 轮 2：用户继续聊——drain 阈值是 'next'，later 被过滤
    resp = agent.chat("跟我聊点别的吧。")
    print(f"[assistant] {resp.text}")
    show_queue_state("after round 2 (later 仍留在队列)")

    saw, _ = last_llm_saw_notification(fake)
    assert not saw, "场景 A 中 LLM 不应该看到后台通知（drain 阈值是 next）"
    assert notification_queue.size() == 1, \
        "later 级通知应该被保留，等 Agent 主动 sleep 或其它触发"

    print("  ✔ 后台完成只入队，不打断用户当前话题")
    print("  ✔ drain 默认阈值 'next' 正确过滤掉 later 通知")
    print("  ✔ 通知仍在队列中等待后续被 SleepTool 或更高优先级触发拉取")


# ---------------------------------------------------------------------------
# 场景 B：Agent 主动 sleep 被通知唤醒
# ---------------------------------------------------------------------------

def demo_scenario_b() -> None:
    section("场景 B：Agent 主动调 SleepTool，后台完成唤醒 → 主动回复用户")
    reset_global_state()

    # 脚本：
    #   轮 1 -> bash run_in_background
    #   轮 2 -> sleep(max_seconds=10)  ← Agent 主动等
    #   轮 3 -> 看到 <task-notification>，向用户播报
    script = [
        {
            "text": "收到，启动后台任务。",
            "tool_use": {
                "name": "bash",
                "input": {
                    "command": "sleep 1 && echo DONE_B",
                    "run_in_background": True,
                },
            },
        },
        {
            "text": "任务启动了，我等它完成。",
            "tool_use": {
                "name": "sleep",
                "input": {
                    "max_seconds": 10,
                    "reason": "waiting for bg task DONE_B",
                },
            },
        },
        {"text": "（本轮 LLM 会看到 <task-notification>，向用户播报完成消息）"},
    ]
    fake = FakeLLM(script=script)
    agent = build_agent(fake)

    show_queue_state("before round 1")
    start = time.monotonic()
    resp = agent.chat("请在后台跑 sleep 1 && echo DONE_B，跑完告诉我。")
    elapsed = time.monotonic() - start
    print(f"[assistant] {resp.text}")
    print(f"[timing] agent.chat() 一共耗时 {elapsed:.2f}s")

    show_queue_state("after round (queue 应被 drain 为空)")

    # 验证 1：整个 chat 不超过 5s（后台任务只要 1s，sleep 应被通知及时唤醒）
    assert elapsed < 5.0, f"SleepTool 应被通知及时唤醒，但耗时 {elapsed:.2f}s"

    # 验证 2：LLM 在最后一轮确实看到了 <task-notification>
    saw, content = last_llm_saw_notification(fake)
    assert saw, "场景 B 中 LLM 应该在最后一轮看到 [background-task-updates]"
    print("\n[LLM 最后一轮看到的通知消息（前 10 行）]")
    for line in content.splitlines()[:10]:
        print(f"  {line}")

    # 验证 3：drain 后队列应为空
    assert notification_queue.size() == 0, "drain 后队列应为空"

    # 验证 4：task 确实 completed
    tasks = background_registry.list_all()
    assert len(tasks) == 1 and tasks[0].status == "completed"

    print("\n  ✔ Agent 主动 sleep 等待异步结果，不占真实 API 轮询")
    print("  ✔ 后台任务完成 → 通知入队 → SleepTool 1s 内轮询醒来")
    print("  ✔ 下一轮 drain 阈值升级到 'later'，通知成功送达 LLM")
    print("  ✔ LLM 自然地向用户播报任务完成——达到'自动回复'的体感")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    demo_scenario_a()
    demo_scenario_b()

    section("DEMO 结束：两个场景的关键断言全部通过 ✔")
    print("场景 A：默认 drain 阈值 'next' 过滤 later → 不打断用户话题")
    print("场景 B：SleepTool 主动轮询 + drain 阈值升级 → 自动回复")
    print("\n一句话总结：")
    print("  把'不打断' 与 '自动播报' 这两种看似矛盾的体感，")
    print("  拆成一个队列 + 两条 drain 路径——控制权始终在 LLM 手上。")


if __name__ == "__main__":
    main()
