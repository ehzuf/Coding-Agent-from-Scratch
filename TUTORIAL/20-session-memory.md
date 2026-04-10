# 从零实现 Coding Agent（二十）：会话记忆

在上一篇中我们实现了 Plan Mode，让 Agent 在规划阶段只能执行只读操作。但随着对话越来越长，一个新的问题浮出水面：**信息丢失**。

当对话历史接近上下文窗口限制时，Auto Compact 会压缩早期消息。压缩虽然释放了空间，但也丢失了细节——用户的核心需求、关键文件路径、之前踩过的坑，这些信息可能在压缩摘要中被遗漏。

本篇实现 **Session Memory（会话记忆）**——一个定期从对话中提取结构化笔记的机制，确保关键信息在压缩时不丢失。

## 设计思路

Session Memory 的核心理念很简单：

1. **定期提取**：每隔 N 次工具调用，用 LLM 分析对话历史，提取/更新一份结构化笔记
2. **笔记模板**：预定义 6 个章节（标题、当前状态、任务需求、关键文件、错误修正、工作日志）
3. **压缩保护**：当 Auto Compact 压缩历史时，笔记作为"不可丢失的上下文"注入到压缩后的消息中

对应 Claude Code 中的 `services/SessionMemory/sessionMemory.ts`，我们的 Python 实现做了简化：不使用 forked sub-agent，而是直接调用 `llm.chat()` 提取笔记。

> **教学简化**：Claude Code 使用 `runForkedAgent` 启动独立子 Agent 来提取笔记，子 Agent 拥有完整的工具能力（如读取文件）。此外 Claude Code 还有 token 阈值（10000 tokens）作为额外触发条件，形成"工具调用次数 + token 增量"的双阈值机制。我们简化为单一的工具调用计数触发 + 直接 `llm.chat()` 提取。

## 笔记模板

```python
SESSION_MEMORY_TEMPLATE = """# Session Title
_简短的会话标题_

# Current State
_当前正在做什么？待完成的任务？下一步计划？_

# Task Specification
_用户要求做什么？关键的设计决策和上下文_

# Key Files
_重要的文件路径及其用途_

# Errors & Corrections
_遇到的错误及修复方式，用户的纠正，应避免的方法_

# Worklog
_逐步记录做了什么，简洁精炼_
"""
```

6 个章节各有分工：

| 章节 | 用途 | 示例 |
|---|---|---|
| Session Title | 一眼看出会话主题 | "实现 Session Memory 功能" |
| Current State | 追踪最新进展 | "已完成 SessionMemory 类，正在集成到 Agent" |
| Task Specification | 保留用户原始需求 | "用户要求实现定期笔记提取，压缩时注入" |
| Key Files | 记住关键文件路径 | "agent/session_memory.py, agent/agent.py" |
| Errors & Corrections | 避免重复犯错 | "LLM 返回文本 < 50 字符时应忽略" |
| Worklog | 完整工作记录 | "1. 创建 SessionMemory 类 2. 添加 Agent 集成" |

## SessionMemory 类

```python
@dataclass
class SessionMemory:
    llm: BaseLLM
    notes: str = ""
    update_interval: int = 3  # 每 3 次工具调用更新一次
    _tool_calls_since_update: int = 0
    _initialized: bool = False
    _update_count: int = 0
```

核心字段：
- `llm`：用于笔记提取的 LLM 实例（复用主 Agent 的 LLM）
- `notes`：当前笔记内容（markdown 格式）
- `update_interval`：触发更新的工具调用间隔，默认 3 次（Claude Code 的 `toolCallsBetweenUpdates` 默认值也是 3）
- `_tool_calls_since_update`：距上次更新的工具调用计数
- `_initialized`：是否已完成首次笔记提取

### 计数与触发

```python
def record_tool_call(self) -> None:
    """记录一次工具调用。"""
    self._tool_calls_since_update += 1

def should_update(self) -> bool:
    """判断是否需要更新笔记。"""
    return self._tool_calls_since_update >= self.update_interval

def maybe_update(self, messages: list[dict]) -> bool:
    """检查并在需要时更新笔记。"""
    if not self.should_update():
        return False
    self.update(messages)
    return True
```

触发机制非常简单：每次工具执行后调用 `record_tool_call()` 累加计数器，Agent 在合适的时机调用 `maybe_update()` 检查是否达到阈值。

### 笔记提取

```python
def update(self, messages: list[dict]) -> None:
    current = self.notes if self.notes else SESSION_MEMORY_TEMPLATE

    # 构建精简的对话摘要
    conversation_summary = self._build_conversation_summary(messages)

    prompt = EXTRACT_PROMPT_TEMPLATE.format(current_notes=current)

    try:
        extract_messages = [
            {"role": "user", "content": conversation_summary},
            {"role": "assistant", "content": "我已阅读对话历史，准备更新笔记。"},
            {"role": "user", "content": prompt},
        ]

        response = self.llm.chat(extract_messages)
        new_notes = response.text.strip()

        if new_notes and len(new_notes) > 50:
            self.notes = new_notes
            self._update_count += 1
    except Exception:
        pass  # 提取失败不影响主流程

    self._tool_calls_since_update = 0
    self._initialized = True
```

几个关键设计：

1. **增量更新**：每次提取都基于当前笔记（`current_notes`），LLM 只需要更新变化的部分
2. **对话摘要**：不传完整消息历史，而是构建精简摘要，控制输入大小
3. **最小长度检查**：`len(new_notes) > 50` 过滤掉明显无效的响应
4. **静默失败**：`except Exception: pass`——笔记提取是辅助功能，不应影响主对话流程
5. **计数器总是重置**：无论提取成功与否，`_tool_calls_since_update` 都重置为 0

### 对话摘要构建

```python
def _build_conversation_summary(self, messages: list[dict]) -> str:
    parts = []
    total_chars = 0
    max_chars = 30000

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        texts.append(f"[调用工具: {block.get('name', '')}]")
                    elif block.get("type") == "tool_result":
                        result = block.get("content", "")
                        if isinstance(result, str) and len(result) > 200:
                            result = result[:200] + "..."
                        texts.append(f"[工具结果: {result}]")
            text = "\n".join(texts)
        else:
            text = str(content)

        if len(text) > 2000:
            text = text[:2000] + "..."

        line = f"[{role}] {text}"
        if total_chars + len(line) > max_chars:
            break
        parts.append(line)
        total_chars += len(line)

    return "\n\n".join(parts)
```

摘要的处理策略：

- **文本消息**：直接保留
- **工具调用**：简化为 `[调用工具: read]` 格式
- **工具结果**：截断到 200 字符（工具输出通常很长）
- **单条消息上限**：2000 字符
- **总量上限**：30000 字符

这样构建的摘要既包含了对话的关键信息，又不会因为大量工具输出而爆炸。

## Agent 集成

Session Memory 需要在三个地方与 Agent 交互。

### 1. 构造函数

```python
class Agent:
    def __init__(
        self,
        # ... 其他参数 ...
        session_memory: SessionMemory | None = None,
    ):
        self.session_memory = session_memory
```

遵循 `session_manager`、`hook_manager` 相同的模式：构造函数参数 → 实例变量。

### 2. 工具执行后计数

在 `_execute_tool()` 中，工具执行完成后记录调用：

```python
def _execute_tool(self, tool_use: dict) -> str:
    # ... 权限检查、Plan Mode 检查、Hook ...

    result = safe_tool_call(tool.call, tool_input, ...)

    # 应用 budget 控制
    if self.enable_budget:
        result = truncate_tool_result(result, self.max_tool_result_length)

    # 记录工具调用（Session Memory 计数器）
    if self.session_memory:
        self.session_memory.record_tool_call()

    # PostToolUse Hook ...
    return result
```

位置在 budget 控制之后、PostToolUse Hook 之前。每次工具成功执行后计数加一。

### 3. 压缩检查时更新笔记

在 `_check_and_compact()` 中，先检查是否需要更新笔记，再检查压缩：

```python
def _check_and_compact(self) -> None:
    if not self.enable_compact:
        return

    # Session Memory：定期更新笔记
    if self.session_memory:
        self.session_memory.maybe_update(self.messages)

    # 检查是否需要压缩
    budget_info = check_context_budget(self.messages, self.compact_threshold)
    if budget_info["is_warning"]:
        new_messages, compact_result = maybe_compact(
            self.messages, self.llm, threshold=self.compact_threshold,
        )
        if compact_result:
            self.messages = new_messages

            # 压缩后注入 Session Memory 笔记
            if self.session_memory:
                notes = self.session_memory.get_notes_for_injection()
                if notes:
                    for i, msg in enumerate(self.messages):
                        if msg.get("role") == "user" and "[历史摘要]" in msg.get("content", ""):
                            self.messages[i]["content"] += "\n\n" + notes
                            break
```

这里有两个关键动作：

1. **更新笔记**（`maybe_update`）：在压缩之前，确保笔记是最新的
2. **注入笔记**：压缩完成后，将笔记追加到 `[历史摘要]` 消息中

为什么注入到历史摘要？因为压缩后的消息结构是：`[system_msg] → [历史摘要] → [最近几轮消息]`。笔记追加到摘要里，LLM 就能看到这些关键信息，不会因为压缩而遗忘。

## build_agent 集成

在 `__main__.py` 的 `build_agent()` 中创建 SessionMemory 实例：

```python
from agent.session_memory import SessionMemory

def build_agent(args) -> tuple[Agent, "Config"]:
    # ... LLM、工具、权限配置 ...

    # Session Memory（会话记忆）
    session_memory = SessionMemory(llm=llm)

    agent = Agent(
        llm=llm,
        tools=tools,
        # ... 其他参数 ...
        session_memory=session_memory,
    )
```

SessionMemory 复用主 Agent 的 LLM 实例，不需要额外配置。

## 序列化与会话恢复

SessionMemory 支持序列化，可以与会话持久化配合：

```python
def to_dict(self) -> dict:
    """序列化为字典。"""
    return {
        "notes": self.notes,
        "update_count": self._update_count,
        "initialized": self._initialized,
    }

@classmethod
def from_dict(cls, data: dict, llm: BaseLLM) -> "SessionMemory":
    """从字典恢复。"""
    sm = cls(llm=llm)
    sm.notes = data.get("notes", "")
    sm._update_count = data.get("update_count", 0)
    sm._initialized = data.get("initialized", False)
    return sm
```

恢复会话时，笔记也会一并恢复，LLM 能立即获得之前的上下文。

## 数据流总结

```
用户输入
  ↓
_check_and_compact()
  ├→ session_memory.maybe_update(messages)    ← 定期提取笔记
  └→ 如果需要压缩:
       compact → 注入 session_memory notes    ← 防止信息丢失
  ↓
LLM 调用
  ↓
工具执行
  ├→ _execute_tool()
  │    └→ session_memory.record_tool_call()   ← 计数
  ↓
回到循环...
```

整个机制在后台静默运行，用户不感知，但长对话的质量因此得到保障。

## 与 Claude Code 的差异

| 方面 | Claude Code | 我们的实现 |
|---|---|---|
| 提取方式 | forked sub-agent（独立上下文） | 直接 llm.chat()（简单直接） |
| 触发条件 | 工具调用 + token 阈值 | 工具调用计数（简化） |
| 笔记注入 | system prompt 动态拼接 | 压缩后注入历史摘要 |
| 并发安全 | 异步执行，不阻塞主流程 | 同步执行（MVP 简化） |

简化的原因：forked sub-agent 需要完整的 Agent 实例和独立消息历史，实现复杂度高。直接调用 `llm.chat()` 虽然会短暂阻塞主流程，但对于 MVP 来说完全够用。

## 小结

Session Memory 解决了长对话中的"信息遗忘"问题：

- **定期提取**：每 3 次工具调用，LLM 自动分析对话，更新结构化笔记
- **压缩保护**：上下文压缩时，笔记作为"不可丢失的上下文"注入
- **静默运行**：整个机制对用户透明，不增加交互负担
- **序列化支持**：笔记随会话持久化，恢复时不丢失

下一篇我们将实现 **Auto-Memory（跨会话记忆）**——让 Agent 在会话结束时提取持久性记忆，在新会话中自动加载，实现真正的"学习"能力。
