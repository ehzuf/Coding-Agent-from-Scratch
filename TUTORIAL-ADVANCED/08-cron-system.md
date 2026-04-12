# 从 Coding Agent 到个人助理（八）：定时任务系统

前面我们构建了一个功能丰富的个人助理——上下文引用、智能路由、成本追踪、信息脱敏、技能自生成、全文搜索、多平台网关。但有个根本限制：**用户不说话，Agent 就不动**。

如果你想让 Agent 每天早上 9 点发一份新闻摘要、每隔 2 小时检查一次邮箱、或者 30 分钟后提醒你开会——就需要一个**定时任务系统**（Cron System）。

本篇实现一个完整的内置 Cron 调度器：JSON 文件存储、4 种时间表达式、多平台交付、数据收集脚本、崩溃安全。最后还会对比 OpenClaw 社区的 Heartbeat 机制，讨论两种自动化策略的取舍。


## Cron vs Heartbeat：两种自动化思路

在进入实现之前，先理清一个概念问题。AI Agent 领域的"定时自动化"有两种主流方案：

**Cron（定时任务）**：独立的调度器，按精确时间触发隔离的 Agent 会话。每个任务有自己的 Prompt、模型、技能、交付目标。类似操作系统的 crontab。

**Heartbeat（心跳）**：在主会话中以固定间隔（如每 30 分钟）唤醒 Agent，让它读取一个任务清单（`HEARTBEAT.md`），批量检查多个事项。Agent 拥有完整的对话上下文，能做上下文感知的决策。

两者的核心区别：

| 维度 | Cron | Heartbeat |
|------|------|-----------|
| 执行环境 | 隔离会话，无上下文 | 主会话，有完整对话历史 |
| 时间精度 | 精确（cron 表达式、ISO 时间戳） | 近似（依赖心跳间隔） |
| 成本模型 | 每个任务独立计费 | 一次心跳批量处理多个检查 |
| 适合场景 | 定时报告、精确提醒、独立分析 | 周期性巡检、邮件检查、上下文感知提醒 |
| 模型选择 | 每个任务可指定不同模型 | 共享主会话模型 |

**Hermes Agent 只实现了 Cron**，没有独立的 Heartbeat 机制。它的迁移文档明确标注：OpenClaw 的 `HEARTBEAT.md` 对应的替代方案是"Use cron jobs for periodic tasks"。这个设计选择背后的逻辑：Cron 是 Heartbeat 的超集——你完全可以用一个 `every 30m` 的 cron 任务配合自定义 Prompt 来模拟心跳行为，但反过来不行。

本篇以 Hermes Agent 的 Cron 实现为蓝本。


## 架构总览

整个 Cron 系统由 4 个模块组成：

```
┌─────────────────────────────────────────────────┐
│  Gateway 进程                                    │
│  ┌────────────┐     ┌──────────────┐            │
│  │ Cron Ticker │────▶│  scheduler   │            │
│  │ (每 60s)    │     │  .tick()     │            │
│  └────────────┘     └──────┬───────┘            │
│                            │                     │
│                     ┌──────▼───────┐            │
│                     │   jobs.py    │            │
│                     │  (JSON 存储)  │            │
│                     └──────────────┘            │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  Agent 工具层                                    │
│  ┌──────────────────┐                           │
│  │ cronjob_tools.py │  create/list/pause/...    │
│  │ (CRONJOB_SCHEMA) │                           │
│  └──────────────────┘                           │
└─────────────────────────────────────────────────┘
```

- **jobs.py**：作业存储与时间表达式解析。一切持久化到 `~/.hermes/cron/jobs.json`
- **scheduler.py**：执行引擎。负责 tick 检测、作业运行、结果交付
- **cronjob_tools.py**：暴露给 Agent 的统一工具接口（单一 `cronjob` 工具，action 分发）
- **gateway/run.py**：后台线程，每 60 秒调用一次 `tick()`


## 时间表达式解析

第一个问题：用户说"30 分钟后提醒我"和"每天早上 9 点"，底层怎么统一处理？

答案是一个 `parse_schedule()` 函数，支持 4 种格式，返回统一的结构化表示：

```python
def parse_schedule(schedule: str) -> dict:
    """
    解析时间表达式，返回统一格式。

    支持 4 种输入：
      "30m"              → 30 分钟后执行一次
      "every 2h"         → 每 2 小时循环执行
      "0 9 * * *"        → 标准 cron 表达式
      "2026-02-03T14:00" → 精确时间点执行一次
    """
    schedule = schedule.strip()

    # 1. "every X" → 循环间隔
    if schedule.lower().startswith("every "):
        minutes = parse_duration(schedule[6:].strip())
        return {"kind": "interval", "minutes": minutes}

    # 2. cron 表达式（5 个空格分隔的字段）
    parts = schedule.split()
    if len(parts) >= 5 and all(
        re.match(r'^[\d\*\-,/]+$', p) for p in parts[:5]
    ):
        croniter(schedule)  # 验证合法性
        return {"kind": "cron", "expr": schedule}

    # 3. ISO 时间戳
    if 'T' in schedule or re.match(r'^\d{4}-\d{2}-\d{2}', schedule):
        dt = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.astimezone()  # 按本地时区解释
        return {"kind": "once", "run_at": dt.isoformat()}

    # 4. 纯时长（"30m"、"2h"、"1d"）→ 从现在开始的一次性任务
    minutes = parse_duration(schedule)
    run_at = now() + timedelta(minutes=minutes)
    return {"kind": "once", "run_at": run_at.isoformat()}
```

`parse_duration()` 负责解析人类友好的时长字符串：

```python
def parse_duration(s: str) -> int:
    """解析时长字符串为分钟数。 '30m' → 30, '2h' → 120, '1d' → 1440"""
    match = re.match(
        r'^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$',
        s.strip().lower()
    )
    if not match:
        raise ValueError(f"Invalid duration: '{s}'")
    value = int(match.group(1))
    unit = match.group(2)[0]
    multipliers = {'m': 1, 'h': 60, 'd': 1440}
    return value * multipliers[unit]
```

设计要点：

- **一次性 vs 循环**由 `kind` 字段区分，不需要用户显式声明
- **cron 表达式**依赖 `croniter` 库做验证和下次执行时间计算——这是个可选依赖，没装就报错提示安装
- **时区处理**：裸时间戳（无时区信息）按本地时区解释，存储时统一转为带时区的 ISO 格式


## 作业存储

作业持久化到一个 JSON 文件：

```
~/.hermes/cron/
├── jobs.json                   # 所有作业定义
├── output/                     # 执行输出
│   └── {job_id}/
│       └── 2026-04-11_09-00-00.md
└── .tick.lock                  # 进程级文件锁
```

单个作业的数据结构：

```python
def create_job(prompt, schedule, name=None, repeat=None,
               deliver=None, origin=None, skills=None,
               model=None, script=None) -> dict:
    parsed = parse_schedule(schedule)

    # 一次性任务默认 repeat=1；循环任务默认无限
    if parsed["kind"] == "once" and repeat is None:
        repeat = 1

    # 默认交付到创建来源（Telegram、CLI 等）
    if deliver is None:
        deliver = "origin" if origin else "local"

    job = {
        "id": uuid.uuid4().hex[:12],
        "name": name or prompt[:50].strip(),
        "prompt": prompt,
        "skills": skills or [],
        "model": model,
        "script": script,            # 数据收集脚本路径
        "schedule": parsed,
        "repeat": {
            "times": repeat,          # None = 无限循环
            "completed": 0
        },
        "enabled": True,
        "state": "scheduled",
        "deliver": deliver,
        "origin": origin,             # 记录创建来源（平台 + chat_id）
        "next_run_at": compute_next_run(parsed),
        "last_run_at": None,
        "last_status": None,
    }

    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)   # 原子写入（先写临时文件，再 rename）
    return job
```

几个值得注意的设计：

**origin 追踪**：每个作业记录它在哪里被创建的（哪个平台、哪个聊天）。当 `deliver="origin"` 时，结果会自动回传到创建来源。如果你在 Telegram 上对 Agent 说"每天 9 点发新闻"，结果就自动发回同一个 Telegram 聊天。

**原子写入**：`save_jobs()` 先写临时文件、`fsync`、再 `os.replace`。中途崩溃不会损坏 jobs.json。

**安全权限**：目录设 `0700`，文件设 `0600`——只有当前用户可读写。


## Tick 循环：60 秒一次的心跳

Gateway 启动时，创建一个后台线程每 60 秒执行一次 `tick()`：

```python
def _start_cron_ticker(stop_event, adapters=None, loop=None, interval=60):
    """后台线程：定期触发 cron 调度检查。"""
    logger.info("Cron ticker started (interval=%ds)", interval)
    tick_count = 0

    while not stop_event.is_set():
        try:
            cron_tick(verbose=False, adapters=adapters, loop=loop)
        except Exception as e:
            logger.debug("Cron tick error: %s", e)

        tick_count += 1

        # 每 5 分钟刷新频道目录
        if tick_count % 5 == 0 and adapters:
            build_channel_directory(adapters)

        # 每小时清理一次文件缓存
        if tick_count % 60 == 0:
            cleanup_image_cache(max_age_hours=24)
            cleanup_document_cache(max_age_hours=24)

        stop_event.wait(timeout=interval)
```

`tick()` 本身使用**文件锁**确保互斥——如果 Gateway 进程和手动触发的 `hermes cron tick` 同时运行，只有一个能拿到锁：

```python
def tick(verbose=True, adapters=None, loop=None) -> int:
    """检查并执行所有到期作业。文件锁确保单实例。"""
    lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # 另一个 tick 正在运行

    try:
        due_jobs = get_due_jobs()
        executed = 0

        for job in due_jobs:
            # 循环作业：执行前先推进 next_run_at（崩溃安全）
            advance_next_run(job["id"])

            success, output, final_response, error = run_job(job)
            save_job_output(job["id"], output)

            # 交付结果
            if should_deliver(final_response):
                delivery_error = _deliver_result(
                    job, final_response,
                    adapters=adapters, loop=loop
                )

            mark_job_run(job["id"], success, error,
                        delivery_error=delivery_error)
            executed += 1

        return executed
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
```

这里有个精妙的**崩溃安全设计**：`advance_next_run()` 在执行**之前**就把循环作业的 `next_run_at` 推进到下一个未来时间点。这样即使进程在执行中崩溃，重启后也不会重复触发同一次运行。一次性作业不做预推进——让它有机会在重启后重试。


## 到期检测与错过处理

`get_due_jobs()` 检查所有启用作业的 `next_run_at`，但有个棘手的边界情况：**如果 Gateway 宕机了 8 小时，那些每小时执行的作业会一次性触发 8 次吗？**

答案是不会——通过 grace window 机制，过期太久的作业会被快进到下一个未来时间：

```python
def get_due_jobs():
    now = _hermes_now()
    due = []

    for job in load_jobs():
        if not job.get("enabled"):
            continue

        next_run_dt = datetime.fromisoformat(job["next_run_at"])
        if next_run_dt <= now:
            schedule = job.get("schedule", {})
            kind = schedule.get("kind")

            # 循环作业的错过检测
            grace = _compute_grace_seconds(schedule)
            if kind in ("cron", "interval"):
                if (now - next_run_dt).total_seconds() > grace:
                    # 超出宽限期 → 快进到下一次
                    new_next = compute_next_run(schedule, now.isoformat())
                    update_job_next_run(job["id"], new_next)
                    continue  # 跳过本次

            due.append(job)

    return due
```

Grace window 的大小与作业频率成正比：每日作业有 2 小时宽限，每小时作业有 30 分钟，10 分钟间隔的作业有 5 分钟。核心思路是：**宁可少跑一次，也不要在重启时爆炸式补跑**。


## 作业执行

每个 cron 作业在一个**完全隔离的 Agent 会话**中执行：

```python
def run_job(job: dict) -> tuple[bool, str, str, Optional[str]]:
    """执行单个 cron 作业。"""
    job_id = job["id"]
    prompt = _build_job_prompt(job)  # 组装完整 Prompt
    session_id = f"cron_{job_id}_{now().strftime('%Y%m%d_%H%M%S')}"

    agent = AIAgent(
        model=resolve_model(job),
        max_iterations=90,
        # 关键：禁用这 3 个工具集
        disabled_toolsets=["cronjob", "messaging", "clarify"],
        quiet_mode=True,
        skip_memory=True,     # 不让 cron 会话污染用户记忆
        platform="cron",
        session_id=session_id,
    )

    # 基于不活动的超时（默认 10 分钟无活动就终止）
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(agent.run_conversation, prompt)

    while True:
        done, _ = wait({future}, timeout=5.0)
        if done:
            break
        idle_secs = agent.get_activity_summary()["seconds_since_activity"]
        if idle_secs >= INACTIVITY_LIMIT:
            agent.interrupt("Cron job timed out (inactivity)")
            raise TimeoutError(...)

    return True, output, final_response, None
```

三个关键设计决策：

**1. 禁用 3 类工具**：
- `cronjob`：防止 cron 作业递归创建新的 cron 作业
- `messaging`：防止自动发消息（结果由交付系统统一处理）
- `clarify`：cron 运行时没有用户在线，不能问问题

**2. skip_memory=True**：cron 作业的系统提示和对话不应该被自动记忆系统捕获，否则会污染用户的个人画像。

**3. 不活动超时**而非绝对超时：一个作业可以跑几个小时（比如执行复杂分析），只要它一直有活动（工具调用、API 请求、流式输出）。但如果 10 分钟没有任何活动，说明可能卡住了，立即终止。


## Prompt 组装

`_build_job_prompt()` 把用户的原始 Prompt 包装成完整的执行上下文：

```python
def _build_job_prompt(job: dict) -> str:
    prompt = job.get("prompt", "")

    # 1. 执行数据收集脚本，注入输出
    script_path = job.get("script")
    if script_path:
        success, output = _run_job_script(script_path)
        if success and output:
            prompt = (
                "## Script Output\n"
                "The following data was collected by a pre-run script.\n\n"
                f"```\n{output}\n```\n\n"
                f"{prompt}"
            )

    # 2. 添加系统指令（告诉 Agent 这是 cron 环境）
    cron_hint = (
        "[SYSTEM: You are running as a scheduled cron job. "
        "DELIVERY: Your final response will be automatically delivered "
        "to the user — do NOT use send_message. "
        "SILENT: If nothing new to report, respond with \"[SILENT]\" "
        "to suppress delivery.]\n\n"
    )
    prompt = cron_hint + prompt

    # 3. 加载指定的 Skill(s)
    for skill_name in job.get("skills", []):
        content = skill_view(skill_name)
        prompt = skill_instruction + content + prompt

    return prompt
```

三层注入：

1. **脚本输出**（可选）：执行前先跑一个 Python 脚本收集数据，把 stdout 注入 Prompt。典型场景：拉取 API 数据、检查文件变化、生成统计报表
2. **系统指令**：告诉 Agent 它在 cron 环境中运行、结果会自动交付、无需手动发消息、无事可报时用 `[SILENT]` 标记抑制交付
3. **Skill 加载**：像正常对话一样加载指定技能


## 数据收集脚本

脚本系统让 cron 作业不只是"问 LLM 一个问题"，而是可以**先收集实时数据，再让 LLM 分析**：

```python
_SCRIPT_TIMEOUT = 120  # 秒

def _run_job_script(script_path: str) -> tuple[bool, str]:
    """执行数据收集脚本，返回 (成功?, 输出)。"""
    # 安全验证：脚本必须在 ~/.hermes/scripts/ 内
    scripts_dir = get_hermes_home() / "scripts"
    resolved = (scripts_dir / Path(script_path)).resolve()
    resolved.relative_to(scripts_dir.resolve())  # 防路径穿越

    result = subprocess.run(
        [sys.executable, str(resolved)],
        capture_output=True, text=True,
        timeout=_SCRIPT_TIMEOUT,
    )

    if result.returncode != 0:
        return False, f"Script exited with code {result.returncode}\n{result.stderr}"

    # 对脚本输出做敏感信息脱敏
    stdout = redact_sensitive_text(result.stdout.strip())
    return True, stdout
```

安全措施：
- **路径限制**：脚本必须位于 `~/.hermes/scripts/`，拒绝绝对路径和 `..` 穿越
- **超时限制**：120 秒强制终止
- **输出脱敏**：脚本输出在注入 LLM Prompt 之前经过信息脱敏处理（复用第四篇的 redact 模块）

典型用法：

```python
# ~/.hermes/scripts/check_github.py
import requests
resp = requests.get(
    "https://api.github.com/repos/myorg/myrepo/issues",
    headers={"Authorization": f"token {os.environ['GITHUB_TOKEN']}"},
    params={"state": "open", "since": yesterday}
)
for issue in resp.json():
    print(f"- #{issue['number']}: {issue['title']}")
```

配合 cron 作业：

```
schedule: "0 9 * * *"
prompt: "分析这些新 Issue，按优先级排序，给出处理建议。"
script: "check_github.py"
```

脚本拉数据，LLM 做分析——各司其职。


## 结果交付

作业执行完后，结果需要送达用户。交付系统支持 12+ 平台，核心逻辑是一个两级回退策略：

```python
def _deliver_result(job, content, adapters=None, loop=None):
    """交付作业结果。优先用活跃适配器，回退到独立 HTTP。"""
    target = _resolve_delivery_target(job)
    if not target:
        return None  # local-only 不交付

    platform = target["platform"]
    chat_id = target["chat_id"]

    # 提取 MEDIA 标签，分离文本和附件
    media_files, cleaned_content = BasePlatformAdapter.extract_media(content)

    # 优先路径：用 Gateway 的活跃适配器（支持 E2EE 加密房间）
    adapter = (adapters or {}).get(platform)
    if adapter and loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            adapter.send(chat_id, cleaned_content), loop
        )
        result = future.result(timeout=60)
        if result.success:
            # 发送附件
            _send_media_via_adapter(adapter, chat_id, media_files, ...)
            return None  # 成功

    # 回退路径：独立 HTTP 发送
    result = asyncio.run(
        _send_to_platform(platform, config, chat_id, cleaned_content)
    )
    return result.get("error")
```

**交付目标解析**支持多种格式：

| deliver 值 | 含义 |
|-----------|------|
| `"origin"` | 回传到创建作业的原始聊天 |
| `"local"` | 仅本地保存，不发送 |
| `"telegram"` | 发送到 Telegram 的 HOME_CHANNEL |
| `"telegram:-1001234:17585"` | 发送到指定群组的指定主题 |
| `"slack:#engineering"` | 发送到 Slack 频道 |

**`[SILENT]` 抑制**：如果 Agent 的最终响应以 `[SILENT]` 开头，说明无事可报，跳过交付。输出仍然保存到本地供审计。这个设计非常重要——一个每 30 分钟检查邮箱的作业，大部分时候没有新邮件，不应该给用户发"没有新邮件"的消息。


## 工具接口

Agent 通过一个统一的 `cronjob` 工具来管理所有定时任务：

```python
CRONJOB_SCHEMA = {
    "name": "cronjob",
    "description": "Manage scheduled cron jobs...",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "create | list | update | pause | resume | remove | run"
            },
            "prompt": {"type": "string"},
            "schedule": {
                "type": "string",
                "description": "'30m', 'every 2h', '0 9 * * *', 或 ISO 时间戳"
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "执行前加载的技能列表"
            },
            "deliver": {
                "type": "string",
                "description": "origin | local | telegram | slack | ..."
            },
            "model": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string"},
                    "model": {"type": "string"}
                }
            },
            "script": {
                "type": "string",
                "description": "数据收集脚本路径（相对于 ~/.hermes/scripts/）"
            },
        },
        "required": ["action"]
    }
}
```

**单一工具、多动作**的设计（action 参数分发）——这是前面教程中讨论过的工具压缩策略。避免注册 7 个独立工具占用上下文预算。

工具入口做了**Prompt 安全扫描**：

```python
_CRON_THREAT_PATTERNS = [
    (r'ignore\s+.*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'curl\s+.*\$\{?\w*(KEY|TOKEN|SECRET)', "exfil_curl"),
    (r'rm\s+-rf\s+/', "destructive_root_rm"),
    # ... 10+ 模式
]

_CRON_INVISIBLE_CHARS = {'\u200b', '\u200c', '\u200d', ...}  # 零宽字符

def _scan_cron_prompt(prompt: str) -> str:
    """扫描 cron prompt 中的威胁模式。"""
    for char in _CRON_INVISIBLE_CHARS:
        if char in prompt:
            return f"Blocked: invisible unicode U+{ord(char):04X}"
    for pattern, pid in _CRON_THREAT_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE):
            return f"Blocked: threat pattern '{pid}'"
    return ""
```

为什么 cron Prompt 需要额外的安全扫描？因为 cron 作业在**无人值守**的环境中运行，拥有完整的工具访问权限（除了前面禁用的 3 个），且没有用户在旁边审查。一个注入攻击可以让 Agent 在凌晨 3 点默默窃取环境变量。


## 完整的教学实现

我们来构建一个精简版的 Cron 系统。核心文件：

```python
# agent/cron.py
"""
精简版 Cron 调度系统。

支持：
- 4 种时间表达式（时长/间隔/cron 表达式/ISO 时间戳）
- JSON 文件持久化
- 多平台交付
- 崩溃安全（预推进 next_run_at）
- [SILENT] 抑制交付
"""

import json
import re
import uuid
import threading
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False


# ── 时间解析 ───────────────────────────────────────────────

def parse_duration(s: str) -> int:
    """'30m' → 30, '2h' → 120, '1d' → 1440"""
    match = re.match(
        r'^(\d+)\s*(m|min|h|hr|hour|d|day)s?$',
        s.strip().lower()
    )
    if not match:
        raise ValueError(f"Invalid duration: '{s}'")
    value = int(match.group(1))
    unit = match.group(2)[0]
    return value * {'m': 1, 'h': 60, 'd': 1440}[unit]


def parse_schedule(schedule: str) -> dict:
    """统一解析 4 种时间表达式。"""
    schedule = schedule.strip()
    now = datetime.now(timezone.utc)

    # "every X" → 循环间隔
    if schedule.lower().startswith("every "):
        minutes = parse_duration(schedule[6:].strip())
        next_run = (now + timedelta(minutes=minutes)).isoformat()
        return {"kind": "interval", "minutes": minutes, "next_run_at": next_run}

    # cron 表达式
    parts = schedule.split()
    if len(parts) >= 5 and all(re.match(r'^[\d\*\-,/]+$', p) for p in parts[:5]):
        if not HAS_CRONITER:
            raise ValueError("Install 'croniter' for cron expressions")
        expr = " ".join(parts[:5])
        croniter(expr)
        it = croniter(expr, now)
        next_run = datetime.fromtimestamp(
            it.get_next(float), tz=timezone.utc
        ).isoformat()
        return {"kind": "cron", "expr": expr, "next_run_at": next_run}

    # ISO 时间戳
    if 'T' in schedule or re.match(r'^\d{4}-\d{2}-\d{2}', schedule):
        dt = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return {"kind": "once", "next_run_at": dt.isoformat()}

    # 时长 → 一次性
    minutes = parse_duration(schedule)
    next_run = (now + timedelta(minutes=minutes)).isoformat()
    return {"kind": "once", "next_run_at": next_run}


def compute_next_run(schedule: dict, after: Optional[str] = None) -> Optional[str]:
    """根据调度配置计算下一次执行时间。"""
    ref = datetime.fromisoformat(after) if after else datetime.now(timezone.utc)

    kind = schedule["kind"]
    if kind == "once":
        return None  # 一次性任务没有下一次

    if kind == "interval":
        minutes = schedule["minutes"]
        return (ref + timedelta(minutes=minutes)).isoformat()

    if kind == "cron":
        it = croniter(schedule["expr"], ref)
        return datetime.fromtimestamp(
            it.get_next(float), tz=timezone.utc
        ).isoformat()

    return None


# ── 作业存储 ───────────────────────────────────────────────

class CronStore:
    """JSON 文件存储，原子写入。"""

    def __init__(self, cron_dir: Path):
        self.cron_dir = cron_dir
        self.jobs_file = cron_dir / "jobs.json"
        self.output_dir = cron_dir / "output"
        self.lock_file = cron_dir / ".tick.lock"
        cron_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[dict]:
        if not self.jobs_file.exists():
            return []
        return json.loads(self.jobs_file.read_text(encoding="utf-8"))

    def save(self, jobs: list[dict]):
        """原子写入：先写临时文件，再 rename。"""
        tmp = self.jobs_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.jobs_file)

    def create_job(self, prompt: str, schedule: str, *,
                   name: str = None, repeat: int = None,
                   deliver: str = "local") -> dict:
        parsed = parse_schedule(schedule)

        if parsed["kind"] == "once" and repeat is None:
            repeat = 1

        job = {
            "id": uuid.uuid4().hex[:12],
            "name": name or prompt[:50].strip(),
            "prompt": prompt,
            "schedule": parsed,
            "repeat": {"times": repeat, "completed": 0},
            "enabled": True,
            "deliver": deliver,
            "next_run_at": parsed["next_run_at"],
            "last_run_at": None,
            "last_status": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        jobs = self.load()
        jobs.append(job)
        self.save(jobs)
        return job

    def get_due_jobs(self) -> list[dict]:
        """获取所有到期作业，对过期循环作业做快进处理。"""
        now = datetime.now(timezone.utc)
        due = []
        jobs = self.load()
        modified = False

        for job in jobs:
            if not job.get("enabled", True):
                continue
            next_run = job.get("next_run_at")
            if not next_run:
                continue

            next_dt = datetime.fromisoformat(next_run)
            if next_dt > now:
                continue

            # 循环作业的过期检测
            kind = job["schedule"]["kind"]
            if kind in ("cron", "interval"):
                staleness = (now - next_dt).total_seconds()
                grace = self._compute_grace(job["schedule"])
                if staleness > grace:
                    # 快进到下一个未来时间
                    new_next = compute_next_run(job["schedule"], now.isoformat())
                    if new_next:
                        job["next_run_at"] = new_next
                        modified = True
                    continue

            due.append(job)

        if modified:
            self.save(jobs)
        return due

    def advance_next_run(self, job_id: str) -> bool:
        """执行前预推进循环作业的 next_run_at（崩溃安全）。"""
        jobs = self.load()
        for job in jobs:
            if job["id"] != job_id:
                continue
            if job["schedule"]["kind"] not in ("cron", "interval"):
                return False
            now = datetime.now(timezone.utc).isoformat()
            new_next = compute_next_run(job["schedule"], now)
            if new_next and new_next != job.get("next_run_at"):
                job["next_run_at"] = new_next
                self.save(jobs)
                return True
            return False
        return False

    def mark_run(self, job_id: str, success: bool, error: str = None):
        """标记作业执行完成，更新计数和状态。"""
        jobs = self.load()
        for i, job in enumerate(jobs):
            if job["id"] != job_id:
                continue

            now = datetime.now(timezone.utc).isoformat()
            job["last_run_at"] = now
            job["last_status"] = "ok" if success else "error"

            repeat = job.get("repeat", {})
            repeat["completed"] = repeat.get("completed", 0) + 1

            # 达到重复上限 → 删除
            times = repeat.get("times")
            if times and repeat["completed"] >= times:
                jobs.pop(i)
                self.save(jobs)
                return

            # 计算下一次
            job["next_run_at"] = compute_next_run(job["schedule"], now)
            if not job["next_run_at"]:
                job["enabled"] = False
            self.save(jobs)
            return

    def save_output(self, job_id: str, content: str) -> Path:
        out_dir = self.output_dir / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        out_file = out_dir / f"{ts}.md"
        out_file.write_text(content, encoding="utf-8")
        return out_file

    @staticmethod
    def _compute_grace(schedule: dict) -> float:
        """宽限期：频率越低，宽限越长。"""
        kind = schedule["kind"]
        if kind == "interval":
            period = schedule["minutes"] * 60
        elif kind == "cron":
            # 粗略估算：通过 croniter 计算两次执行的间隔
            try:
                it = croniter(schedule["expr"])
                t1 = it.get_next(float)
                t2 = it.get_next(float)
                period = t2 - t1
            except Exception:
                period = 3600
        else:
            return 120  # 一次性作业 2 分钟宽限

        # 宽限 = period 的 50%，最小 5 分钟，最大 2 小时
        grace = max(300, min(period * 0.5, 7200))
        return grace


# ── 调度器 ─────────────────────────────────────────────────

SILENT_MARKER = "[SILENT]"


class CronScheduler:
    """Cron 调度器：检测到期作业，执行 Agent，交付结果。"""

    def __init__(self, store: CronStore, agent_factory=None):
        """
        agent_factory: 可调用对象，接收 (prompt, **kwargs) 返回 Agent 响应字符串。
                       默认用简单的 echo 实现。
        """
        self.store = store
        self.agent_factory = agent_factory or self._default_agent
        self._stop_event = threading.Event()
        self._ticker_thread = None

    @staticmethod
    def _default_agent(prompt, **kwargs):
        return f"[CronAgent] Executed prompt: {prompt[:100]}"

    def tick(self) -> int:
        """执行一次调度检查。"""
        due_jobs = self.store.get_due_jobs()
        executed = 0

        for job in due_jobs:
            try:
                # 崩溃安全：循环作业预推进
                self.store.advance_next_run(job["id"])

                # 组装 Prompt
                cron_hint = (
                    "[SYSTEM: You are running as a scheduled cron job. "
                    "If nothing to report, respond with [SILENT].]\n\n"
                )
                full_prompt = cron_hint + job["prompt"]

                # 执行 Agent
                response = self.agent_factory(full_prompt)
                success = True
                error = None
            except Exception as e:
                response = f"Error: {e}"
                success = False
                error = str(e)

            # 保存输出
            output = f"# Cron: {job['name']}\n\n{response}"
            self.store.save_output(job["id"], output)

            # 交付判断
            should_deliver = bool(response) and success
            if should_deliver and SILENT_MARKER in response.strip().upper():
                should_deliver = False  # Agent 说无事可报

            if should_deliver and job.get("deliver", "local") != "local":
                self._deliver(job, response)

            self.store.mark_run(job["id"], success, error)
            executed += 1

        return executed

    def _deliver(self, job: dict, content: str):
        """交付结果到目标平台。简化版只打印。"""
        deliver = job.get("deliver", "local")
        print(f"[Deliver → {deliver}] {job['name']}: {content[:200]}")

    def start(self, interval: int = 60):
        """启动后台 ticker 线程。"""
        self._stop_event.clear()

        def _loop():
            while not self._stop_event.is_set():
                try:
                    self.tick()
                except Exception as e:
                    print(f"Tick error: {e}")
                self._stop_event.wait(timeout=interval)

        self._ticker_thread = threading.Thread(target=_loop, daemon=True)
        self._ticker_thread.start()

    def stop(self):
        """停止后台 ticker。"""
        self._stop_event.set()
        if self._ticker_thread:
            self._ticker_thread.join(timeout=5)
```

用法演示：

```python
from pathlib import Path
from agent.cron import CronStore, CronScheduler

# 初始化
store = CronStore(Path("./data/cron"))

# 创建作业
job1 = store.create_job(
    prompt="检查今天的 GitHub 通知，总结重要的。",
    schedule="every 2h",
    name="GitHub 巡检",
    deliver="telegram"
)

job2 = store.create_job(
    prompt="30 分钟后提醒我给产品经理回邮件。",
    schedule="30m",
    name="邮件提醒"
)

job3 = store.create_job(
    prompt="汇总本周的代码提交，生成周报。",
    schedule="0 17 * * 5",  # 每周五下午 5 点
    name="周报生成",
    deliver="slack:#team"
)

# 挂载真正的 Agent
def run_agent(prompt, **kwargs):
    from agent.agent import Agent
    from agent.llm.openai_llm import OpenAILLM
    llm = OpenAILLM(model="gpt-4o-mini")
    agent = Agent(llm=llm)
    return agent.run(prompt)

scheduler = CronScheduler(store, agent_factory=run_agent)
scheduler.start(interval=60)  # 每 60 秒检查一次
```


## Hermes Agent 的额外工程化设计

教学版本覆盖了核心机制。Hermes Agent 在此基础上增加了以下工程化考量：

**1. 脚本注入系统**：每个作业可以绑定一个 Python 脚本（位于 `~/.hermes/scripts/`），在 Agent 运行前执行。脚本的 stdout 作为上下文注入 Prompt。路径做了严格的穿越验证。

**2. 多模型支持**：每个作业可以指定不同的 model/provider/base_url，甚至走不同的 API 端点。配合智能路由（第二篇），低优先级作业可以用便宜模型。

**3. 双层交付**：优先用 Gateway 的活跃适配器发送（支持 Matrix E2EE 加密房间），失败后回退到独立 HTTP 发送。

**4. 安全扫描**：创建作业时扫描 Prompt 中的威胁模式（注入攻击、凭证泄露、破坏性命令）和零宽字符。

**5. 跨平台文件锁**：Unix 用 `fcntl.flock`，Windows 用 `msvcrt.locking`，确保多进程不会同时 tick。

**6. 环境变量隔离**：每个作业执行时注入的环境变量（`HERMES_SESSION_PLATFORM` 等）在 `finally` 块中清理，防止泄漏到下一个作业。


## 再谈 Heartbeat

回到开篇的问题：OpenClaw 的 Heartbeat 具体怎么工作？

1. 用户创建一个 `~/.openclaw/workspace/HEARTBEAT.md` 文件，列出周期性检查项
2. 系统每 30 分钟在**主会话**中唤醒 Agent，让它读取这个文件并逐项执行
3. Agent 有完整的对话历史，能做上下文感知的判断（比如"用户刚才说今天不想被打扰"→ 跳过非紧急通知）
4. 如果无事可报，Agent 返回 `HEARTBEAT_OK`，不打扰用户

```markdown
# HEARTBEAT.md 示例
- 检查邮件中的紧急消息
- 查看未来 2 小时的日历事件
- 如果闲置超过 8 小时，发送简短问候
```

**Heartbeat 的优势**：

- **成本低**：一次心跳处理多个检查项，比 5 个独立 cron 便宜
- **上下文感知**：Agent 知道最近聊了什么，能智能过滤
- **配置简单**：写个 Markdown 文件就行

**Heartbeat 的局限**：

- **时间不精确**：依赖 30 分钟的心跳间隔，做不到"下午 2:17 准时提醒"
- **不能隔离**：共享主会话，一个检查项出错可能影响其他
- **不能指定模型**：所有检查项用同一个模型

**Hermes Agent 的选择**是不单独实现 Heartbeat，而是用 Cron 覆盖所有场景。如果你想要 Heartbeat 的行为，创建一个 `every 30m` 的 cron 作业、Prompt 里列出检查清单即可。虽然少了主会话上下文，但换来了隔离性和灵活性。

这是一个合理的工程取舍——两种方案都有效，选择取决于你更看重"成本/上下文"还是"精确/隔离"。


## 小结

定时任务系统让 Agent 从"被动问答"升级为"主动服务"：

- **4 种时间表达式**统一了"30 分钟后"和"每天早上 9 点"的表达
- **JSON 文件存储** + 原子写入提供了简单可靠的持久化
- **崩溃安全设计**：预推进 `next_run_at` + 过期快进，避免重复执行和积压爆炸
- **`[SILENT]` 抑制**让周期性检查不会变成垃圾通知
- **数据收集脚本**让 cron 不只是问问题，而是先收数据再分析
- **安全扫描**防止无人值守环境下的 Prompt 注入
