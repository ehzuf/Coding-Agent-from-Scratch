"""
Coordinator 模式 —— 让主 Agent 专注于编排子 Agent

对应 Claude Code 源码中的 coordinator/coordinatorMode.ts

核心思想：
  Coordinator 模式下，主 Agent 不直接执行工具，而是将任务拆解后
  分配给多个子 Agent（Worker）并行执行，最后综合结果回复用户。

  这是 fan-out / fan-in 模式：
    1. fan-out: 主 Agent 同时启动多个子 Agent
    2. 各子 Agent 独立工作（并发执行）
    3. fan-in: 主 Agent 收集所有结果，综合回复

关键组件：
  - COORDINATOR_SYSTEM_PROMPT: 告诉 LLM 扮演"协调者"角色
  - build_coordinator_context(): 构建 Worker 可用工具信息
  - format_task_notification(): 将子 Agent 结果格式化为结构化通知
"""


# Coordinator 专用系统提示
# 对应 Claude Code 的 getCoordinatorSystemPrompt()
COORDINATOR_SYSTEM_PROMPT = """你是一个 AI 编程助手，负责协调多个 Worker 完成软件工程任务。

## 1. 你的角色

你是 **Coordinator（协调者）**。你的职责是：
- 理解用户目标，将任务拆解为可并行的子任务
- 启动 Worker（子 Agent）执行具体工作
- 综合 Worker 的结果，向用户汇报

每条消息都是给用户的。Worker 的结果是内部信号，不是对话伙伴——不要感谢或确认 Worker，直接总结信息给用户。

## 2. 你的工具

- **agent** — 启动一个新的 Worker
- **send_message** — 继续一个已有的 Worker（发送后续指令）

使用 agent 工具时：
- 不要让一个 Worker 去检查另一个 Worker 的状态
- 不要用 Worker 做简单的单步操作（直接回答更高效）
- 启动 Worker 后，简要告诉用户你启动了什么，然后结束回复
- 不要编造或预测 Worker 结果

## 3. 任务工作流

大多数任务可以分为以下阶段：

| 阶段 | 执行者 | 目的 |
|------|--------|------|
| 调研 | Worker（并行） | 探索代码库，理解问题 |
| 综合 | **你（Coordinator）** | 阅读调研结果，制定实施方案 |
| 实施 | Worker | 按照方案执行修改 |
| 验证 | Worker | 测试修改是否正确 |

## 4. 并发是你的优势

**尽量并行启动 Worker。** 不要串行化可以并行的工作。
- **只读任务**（调研）— 自由并行
- **写入任务**（实施）— 同一组文件不要并发修改
- **验证**可以和其他文件区域的实施同时进行

要并行启动 Worker，在一次回复中调用多个 agent 工具。

## 5. 编写 Worker 指令

**Worker 看不到你和用户的对话。** 每个指令必须自包含：
- 包含具体的文件路径、行号、错误信息
- 说明什么算"完成"
- 调研类任务加上"不要修改文件"
- 实施类任务加上"完成后汇报修改的文件"

好的例子：
  "修复 src/auth/validate.py:42 的空指针。Session.user 在会话过期时为 None，\
在访问 user.id 前加空值检查，返回 401 错误。"

差的例子：
  "修复那个 bug"（没有上下文）
  "根据你的发现修复问题"（偷懒委托）"""


# Worker 专用系统提示（覆盖默认的子 Agent 提示）
WORKER_SYSTEM_PROMPT = """你是一个执行具体任务的 Worker。

规则：
1. 严格完成分配给你的任务，不要偏离
2. 使用工具直接执行，不要闲聊
3. 完成后给出简洁的结果报告
4. 如果任务是调研，只报告发现，不要修改文件
5. 如果任务是实施，报告修改的文件和关键变更"""


def build_coordinator_context(tool_names: list[str]) -> str:
    """
    构建 Coordinator 的额外上下文：Worker 可用的工具信息。

    对应 Claude Code 的 getCoordinatorUserContext()

    Args:
        tool_names: Worker 可用的工具名称列表

    Returns:
        上下文字符串，描述 Worker 的能力
    """
    worker_tools = ", ".join(sorted(tool_names))
    return f"\nWorker 可用的工具: {worker_tools}"


def format_task_notification(
    agent_id: str,
    description: str,
    status: str,
    content: str,
    turns: int = 0,
    tool_uses: int = 0,
    duration_ms: int = 0,
    total_input_tokens: int = 0,
    total_output_tokens: int = 0,
) -> str:
    """
    将子 Agent 结果格式化为结构化 task-notification。

    对应 Claude Code 的 enqueueAgentNotification() 中的 XML 格式。
    使用 <task-notification> XML 标签，方便 Coordinator LLM 解析。

    Args:
        agent_id:     子 Agent 的唯一标识
        description:  任务描述
        status:       执行状态 (completed/failed)
        content:      结果文本
        turns:        执行轮数
        tool_uses:    工具调用次数
        duration_ms:  执行耗时（毫秒）
        total_input_tokens:  累计输入 token
        total_output_tokens: 累计输出 token

    Returns:
        格式化的 task-notification XML 字符串
    """
    parts = [
        "<task-notification>",
        f"<task-id>{agent_id}</task-id>",
        f"<description>{description}</description>",
        f"<status>{status}</status>",
        f"<result>\n{content}\n</result>",
        "<usage>",
        f"  <turns>{turns}</turns>",
        f"  <tool_uses>{tool_uses}</tool_uses>",
        f"  <duration_ms>{duration_ms}</duration_ms>",
        f"  <total_input_tokens>{total_input_tokens}</total_input_tokens>",
        f"  <total_output_tokens>{total_output_tokens}</total_output_tokens>",
        "</usage>",
        "</task-notification>",
    ]
    return "\n".join(parts)
