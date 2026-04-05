# 从零实现 Coding Agent（十六）：会话持久化

在前面十五篇中，我们的 Agent 已经具备了工具调用、多轮对话、子 Agent 协作、Coordinator 编排等能力。但有一个根本性的问题：**每次退出程序，所有对话历史都会丢失**。

下次打开 Agent，它对之前的工作一无所知。如果一个复杂任务做到一半中断了（网络掉线、不小心关了终端、需要暂时去做别的事），就只能从头开始。

本篇实现 **会话持久化（Session Persistence）**：把消息历史写入磁盘，支持 `--resume` 从中断处继续。

这对应 Claude Code 源码中的 `history.ts` + `sessionStorage.ts` + `sessionRestore.ts`。

---

## 为什么需要会话持久化？

先看一个真实痛点。你让 Agent 重构一个模块：

```
你: 重构 agent/config.py，拆分为多个文件
Agent: 好的，我先分析现有结构...
  → [read] 读取 config.py
  → [grep] 搜索所有引用
  → [write] 创建 config/base.py
  → [write] 创建 config/env.py
  → [edit] 修改 __init__.py
Agent: 已完成文件拆分，接下来更新引用...
```

这时你的网络断了。或者你需要去开个会。重新打开 Agent——它什么都不记得了，你要重新描述上下文，甚至之前已经完成的中间步骤也需要重新确认。

有了会话持久化：

```bash
# 上次退出时显示的会话 ID
python -m agent --resume abc12345
# Agent 恢复全部消息历史，从中断处继续
```

---

## 核心设计：JSONL 追加写入

### 为什么选 JSONL？

存储消息历史有几种选择：

| 格式 | 优点 | 缺点 |
|------|------|------|
| SQLite | 查询灵活 | 对追加写入场景过重，依赖外部库 |
| 单个 JSON 文件 | 简单 | 每次写入要重写整个文件，文件越大越慢 |
| **JSONL（每行一条 JSON）** | **追加写入、逐行解析** | 不便于随机查询 |
| pickle | 最快的序列化 | 不可读，版本不兼容 |

JSONL 是最适合这个场景的格式：

1. **追加写入**：新消息直接 append 到文件末尾，O(1) 操作
2. **逐行解析**：恢复时逐行读取，单行 JSON 解析失败不影响其他行
3. **可读可调试**：用任何文本编辑器都能查看历史
4. **Claude Code 也是这么做的**：`history.jsonl`

### JSONL 文件结构

```jsonl
{"type": "meta", "session_id": "abc12345", "project": "/path/to/project", "model": "claude-sonnet-4-20250514", "created_at": 1712249400.0, "updated_at": 1712250000.0, "message_count": 4, "first_prompt": "重构 config.py"}
{"type": "message", "timestamp": 1712249401.0, "role": "user", "content": "重构 agent/config.py"}
{"type": "message", "timestamp": 1712249410.0, "role": "assistant", "content": [{"type": "text", "text": "好的，让我先分析..."}]}
{"type": "message", "timestamp": 1712249415.0, "role": "user", "content": [{"type": "tool_result", "tool_use_id": "xxx", "content": "..."}]}
{"type": "message", "timestamp": 1712249420.0, "role": "assistant", "content": [{"type": "text", "text": "分析完成..."}]}
```

关键设计：
- **首行是元数据**：`type: "meta"`，包含 session_id、project、model、消息数、第一条 prompt 预览
- **后续每行是消息**：`type: "message"`，原样保存 Agent 的消息 dict
- **元数据首行会更新**：每次追加消息后重写首行，更新 `updated_at` 和 `message_count`

首行元数据的好处：`--list-sessions` 只需要读取每个文件的第一行就能展示会话列表，不需要解析整个文件。

---

## 实现

### SessionManager 类

`agent/session.py` 的核心是 `SessionManager` 类：

```python
class SessionManager:
    def __init__(
        self,
        session_id: str | None = None,
        project: str = "",
        model: str = "",
    ):
        self.session_id = session_id or generate_session_id()
        self.sessions_dir = _get_sessions_dir()  # ~/.coding-agent/sessions/
        self.session_path = self.sessions_dir / f"{self.session_id}.jsonl"
```

三个核心方法：

**1. 追加消息（写入）**

```python
def append_message(self, message: dict) -> None:
    self._ensure_file()  # 首次调用时创建文件并写入元数据首行
    entry = {
        "type": "message",
        "timestamp": time.time(),
        **message,
    }
    with open(self.session_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    self._message_count += 1
    self._update_meta()  # 更新首行元数据
```

关键点：
- `open(..., "a")` —— append 模式，只追加不覆盖
- `ensure_ascii=False` —— 保留中文原文，不转义为 `\uXXXX`
- 消息以原始 dict 格式存储，兼容 Anthropic 和 OpenAI 两种消息格式

**2. 加载消息（读取）**

```python
def load_messages(self) -> list[dict]:
    messages = []
    with open(self.session_path, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line.strip())
            if entry.get("type") == "meta":
                self._meta = SessionMeta.from_dict(entry)
                continue
            if entry.get("type") == "message":
                # 去掉 type 和 timestamp 字段，还原标准消息格式
                msg = {k: v for k, v in entry.items()
                       if k not in ("type", "timestamp")}
                messages.append(msg)
    return messages
```

还原时去掉 `type` 和 `timestamp`，恢复的消息和 Agent 运行时的格式完全一致。

**3. 恢复会话（类方法）**

```python
@classmethod
def resume(cls, session_id: str) -> "SessionManager":
    sessions_dir = _get_sessions_dir()
    session_path = sessions_dir / f"{session_id}.jsonl"
    if not session_path.exists():
        raise FileNotFoundError(f"会话 '{session_id}' 不存在")
    sm = cls(session_id=session_id)
    # 读取元数据
    with open(session_path, "r") as f:
        first_line = f.readline().strip()
        meta_data = json.loads(first_line)
        if meta_data.get("type") == "meta":
            sm._meta = SessionMeta.from_dict(meta_data)
    return sm
```

### Agent 集成

Agent 只需要增加三行代码就能支持持久化：

```python
class Agent:
    def __init__(self, ..., session_manager: SessionManager | None = None):
        self.session_manager = session_manager

    def _persist_message(self, message: dict) -> None:
        if self.session_manager:
            self.session_manager.append_message(message)

    def _persist_messages(self, messages: list[dict]) -> None:
        if self.session_manager:
            self.session_manager.append_messages(messages)
```

然后在三个消息追加点调用：

```python
def _run_tool_loop(self, prompt):
    # 1. 用户消息
    self.messages.append({"role": "user", "content": prompt})
    self._persist_message(self.messages[-1])

    while turn_count < self.max_turns:
        response = self._call_llm_with_retry(api_tools)

        # 2. Assistant 消息
        assistant_msg = self._build_assistant_message(response)
        self.messages.append(assistant_msg)
        self._persist_message(assistant_msg)

        if not response.has_tool_use:
            return response

        # 3. Tool result 消息
        results = self._execute_tools(response.tool_uses)
        tool_result_messages = self._build_tool_result_messages(...)
        self.messages.extend(tool_result_messages)
        self._persist_messages(tool_result_messages)
```

恢复时，从 JSONL 文件加载消息并注入 Agent：

```python
def restore_messages(self, messages: list[dict]) -> None:
    self.messages = messages
```

### CLI 集成

```python
# __main__.py
parser.add_argument("--resume", metavar="SESSION_ID")
parser.add_argument("--list-sessions", action="store_true")
parser.add_argument("--no-session", action="store_true")
```

`build_agent()` 中自动创建或恢复 `SessionManager`：

```python
if config.enable_session:
    if config.resume_session:
        session_manager = SessionManager.resume(config.resume_session)
    else:
        session_manager = SessionManager(project=cwd_abs, model=model)
```

---

## 与 Claude Code 的对比

| 维度 | Claude Code | 我们的实现 |
|------|-------------|------------|
| 存储格式 | JSONL | JSONL |
| 存储位置 | `~/.claude/history.jsonl` | `~/.coding-agent/sessions/{id}.jsonl` |
| 并发控制 | lockfile（多进程安全） | 单进程（无需锁） |
| 大内容处理 | hash → paste store（>1024字符外存） | 直接内联（简化） |
| 会话粒度 | 单文件 + sessionId 过滤 | 每会话独立文件 |
| 恢复范围 | messages + file history + attribution + todos | messages（核心） |
| 异步写入 | pending buffer + async flush | 同步追加（简单可靠） |

我们的实现更简单：
- **每会话一个文件**（vs Claude Code 的全局单文件 + sessionId 过滤），查找和管理更直观
- **同步写入**（vs async flush with pending buffer），不会丢失消息
- **只恢复消息**（vs 恢复 file history、attribution、todos 等），MVP 足够

Claude Code 更复杂的原因：
- 需要跨进程共享历史（多个 Claude Code 实例同时运行）
- 需要处理超大粘贴内容（hash 引用 + 外部存储）
- 需要恢复完整的 IDE 状态（file history、attribution 等）

---

## 会话文件管理

### 存储目录

```
~/.coding-agent/
└── sessions/
    ├── abc12345.jsonl    # 会话 1
    ├── def67890.jsonl    # 会话 2
    └── ghi24680.jsonl    # 会话 3
```

### 列出会话

`--list-sessions` 遍历目录，读取每个文件的首行元数据：

```python
@staticmethod
def list_sessions(project=None, limit=20):
    metas = []
    for path in sessions_dir.glob("*.jsonl"):
        with open(path) as f:
            first_line = f.readline().strip()
            data = json.loads(first_line)
            if data.get("type") == "meta":
                meta = SessionMeta.from_dict(data)
                if project and meta.project != project:
                    continue
                metas.append(meta)
    metas.sort(key=lambda m: m.updated_at, reverse=True)
    return metas[:limit]
```

按 `updated_at` 降序排列，最近活跃的会话排在前面。支持按项目路径过滤。

### Session ID

使用 UUID 前 8 位作为 session_id：

```python
def generate_session_id() -> str:
    return uuid.uuid4().hex[:8]
```

8 位十六进制 = 16^8 ≈ 43 亿种可能，对于本地使用完全够用。短 ID 方便记忆和手动输入。

---

## 使用示例

### 基本工作流

```bash
# 开始新对话
$ python -m agent "分析 agent/tools/ 目录的代码结构"
[anthropic / claude-sonnet-4-20250514] 工作目录: /Users/xxx/project
  会话 ID: abc12345
# ... Agent 分析代码 ...

# 退出后想继续
$ python -m agent --resume abc12345
  恢复会话: abc12345
  已恢复 12 条消息
# Agent 记得之前的对话内容
[7] 你: 刚才提到的 base.py 能详细讲讲吗？
```

### 交互式查看

```bash
$ python -m agent
[1] 你: /session
--- 当前会话 ---
  会话 ID: abc12345
  项目: /Users/xxx/project
  模型: claude-sonnet-4-20250514
  消息数: 6
  恢复命令: python -m agent --resume abc12345
```

### 列出所有会话

```bash
$ python -m agent --list-sessions
可用会话（共 3 个）:
  abc12345  2026-04-04 22:30  [12 条消息]  分析 agent/tools/ 目录的代码结构
  def67890  2026-04-04 21:15  [6 条消息]   重构 config.py
  ghi24680  2026-04-04 20:00  [4 条消息]   修复 permission 模块的 bug
```

---

## 小结

这一篇实现了会话持久化：

1. **SessionManager** —— JSONL 格式的会话存储管理器
2. **追加写入** —— 每条消息 append 到文件，不会丢失中间状态
3. **元数据首行** —— 快速扫描会话列表，不需要解析整个文件
4. **Agent 无感集成** —— 三个 `_persist_message` 调用点，不影响现有逻辑
5. **`--resume` 恢复** —— 从 JSONL 加载消息，注入 Agent，从中断处继续

本质上，会话持久化解决的问题很简单：**LLM 是无状态的，对话上下文全靠消息历史。只要能把消息历史序列化到磁盘并还原回来，Agent 就能"记住"之前的对话。**

下一篇将实现 Hooks 系统——工具调用前后执行用户自定义脚本，为 Agent 提供可扩展的生命周期钩子。
