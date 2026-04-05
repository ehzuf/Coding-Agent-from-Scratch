# 从零实现 Coding Agent（二十一）：跨会话记忆

上一篇实现了 Session Memory（会话记忆），解决了单次对话中的信息丢失问题。但当用户关闭终端、开始新会话时，所有上下文都会丢失——Agent 不记得用户的偏好、项目的约定、上次踩过的坑。

本篇实现 **Auto-Memory（跨会话记忆）**——让 Agent 在对话结束时自动提取持久性记忆，在新会话中加载，实现真正的"学习"能力。

## 设计思路

Auto-Memory 的核心流程：

1. **对话结束时提取**：LLM 最终回复后（无工具调用），用 LLM 分析对话，提取值得记住的信息
2. **持久化存储**：每条记忆保存为独立的 `.md` 文件，带 frontmatter 元数据
3. **索引管理**：`MEMORY.md` 作为索引，列出所有记忆
4. **新会话加载**：启动时读取 `MEMORY.md`，注入 system prompt

对应 Claude Code 中的 `memdir/` + `services/extractMemories/`。Claude Code 使用 forked sub-agent 做提取，我们简化为直接 `llm.chat()` 调用。

> **教学简化**：Claude Code 的记忆提取使用 `runForkedAgent`，子 Agent 拥有文件读写工具（如 `createMemoryFileCanUseTool()`），可以直接操作记忆文件。我们简化为让主 LLM 返回 JSON 格式的记忆内容，再由 Python 代码保存文件，降低实现复杂度。

## 记忆目录结构

```
~/.coding-agent/
  projects/
    <path-hash>/      # SHA256(项目绝对路径) 前 12 位
      memory/
        MEMORY.md     # 索引文件
        user_prefs.md # 用户偏好
        project_stack.md  # 项目技术栈
        feedback_testing.md  # 测试反馈
```

每个项目有独立的记忆目录。路径 hash 用 SHA256 前 12 位，确保同一项目始终映射到同一目录：

```python
def _project_hash(cwd: str) -> str:
    abs_path = os.path.abspath(cwd)
    return hashlib.sha256(abs_path.encode()).hexdigest()[:12]

def get_memory_dir(cwd: str) -> str:
    return os.path.join(PROJECTS_DIR, _project_hash(cwd), MEMORY_DIRNAME)
```

## 四种记忆类型

参考 Claude Code 的四种分类：

| 类型 | 用途 | 示例 |
|---|---|---|
| **user** | 用户信息和偏好 | "用户偏好 Python 类型注解"、"用户是后端开发" |
| **feedback** | 用户反馈和纠正 | "不要用 os.path，改用 pathlib"、"测试要用 pytest" |
| **project** | 项目事实 | "技术栈是 Python 3.10 + FastAPI"、"配置在 config.py" |
| **reference** | 外部引用 | "API 文档在 docs.example.com"、"部署脚本在 scripts/" |

关键原则：**只记住跨会话有用的信息**。代码中直接能看到的信息（函数签名、import 路径）不需要记忆。

## 记忆文件格式

每条记忆是一个带 frontmatter 的 markdown 文件：

```markdown
---
name: User Preferences
description: User coding preferences
type: user
---

Prefers Python with type hints.
Uses pathlib instead of os.path.
Commit messages should be in Chinese.
```

frontmatter 使用简化解析（不引入 YAML 依赖）：

```python
def _parse_frontmatter(content: str) -> dict:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}

    data = {}
    for line in match.group(1).split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip()
    return data
```

## 记忆扫描

`scan_memory_files()` 扫描记忆目录，读取所有 `.md` 文件的 frontmatter：

```python
def scan_memory_files(memory_dir: str) -> list[MemoryEntry]:
    entries = []
    for filename in os.listdir(memory_dir):
        if not filename.endswith(".md") or filename == MEMORY_INDEX:
            continue
        filepath = os.path.join(memory_dir, filename)
        content = Path(filepath).read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        entries.append(MemoryEntry(
            filename=filename,
            filepath=filepath,
            name=fm.get("name", filename),
            description=fm.get("description", ""),
            memory_type=fm.get("type", ""),
            content=content,
        ))
    # 按修改时间降序排列
    entries.sort(key=lambda e: os.path.getmtime(e.filepath), reverse=True)
    return entries[:MAX_MEMORY_FILES]
```

排除 `MEMORY.md`（索引文件），按修改时间排序，上限 100 条。

## 记忆提取

核心方法 `extract_and_save()` 在对话结束时调用：

```python
def extract_and_save(self, messages: list[dict]) -> int:
    if len(messages) < 4:
        return 0  # 对话太短不提取

    # 1. 扫描已有记忆（供去重）
    memory_dir = ensure_memory_dir(self.cwd)
    existing = scan_memory_files(memory_dir)
    existing_manifest = format_memory_manifest(existing)

    # 2. 构建对话摘要
    summary = self._build_conversation_summary(messages)

    # 3. 调用 LLM 提取
    prompt = EXTRACT_PROMPT.format(existing_memories=existing_manifest)
    extract_messages = [
        {"role": "user", "content": summary},
        {"role": "assistant", "content": "我已阅读对话历史，准备提取持久性记忆。"},
        {"role": "user", "content": prompt},
    ]
    response = self.llm.chat(extract_messages)

    # 4. 解析 JSON 结果
    memories = self._parse_memories(response.text)

    # 5. 保存文件 + 重建索引
    saved = 0
    for mem in memories:
        if self._save_memory(memory_dir, mem):
            saved += 1
    if saved > 0:
        self._update_index(memory_dir)
    return saved
```

### 提取 Prompt

提取 prompt 的核心指导：

1. **四种类型**：明确定义每种类型的范围和示例
2. **不应保存**：排除可推断信息、临时信息、敏感信息
3. **去重**：传入已有记忆清单，要求 LLM 检查避免重复
4. **输出格式**：JSON 数组，每个元素包含 filename、name、description、type、content

### JSON 解析

LLM 返回的 JSON 可能被 markdown 代码块包裹：

```python
def _parse_memories(self, text: str) -> list[dict]:
    import json
    # 尝试从代码块中提取
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    json_str = match.group(1) if match else text
    try:
        result = json.loads(json_str)
        if isinstance(result, list):
            return [m for m in result if isinstance(m, dict) and m.get("filename")]
        return []
    except (json.JSONDecodeError, ValueError):
        return []
```

### 索引重建

每次保存后重建 `MEMORY.md`，按类型分组：

```python
def _update_index(self, memory_dir: str) -> None:
    entries = scan_memory_files(memory_dir)
    # 按类型分组
    by_type: dict[str, list[MemoryEntry]] = {}
    for e in entries:
        t = e.memory_type or "other"
        by_type.setdefault(t, []).append(e)

    type_labels = {
        "user": "用户偏好", "feedback": "反馈与纠正",
        "project": "项目事实", "reference": "参考引用",
    }

    lines = ["# 项目记忆索引", ""]
    for t in ["user", "feedback", "project", "reference", "other"]:
        group = by_type.get(t)
        if not group:
            continue
        lines.append(f"## {type_labels.get(t, t)}")
        for e in group:
            desc = f" — {e.description}" if e.description else ""
            lines.append(f"- [{e.name}]({e.filename}){desc}")
```

生成的 `MEMORY.md` 示例：

```markdown
# 项目记忆索引

## 用户偏好

- [User Preferences](user_prefs.md) — User coding preferences

## 项目事实

- [Tech Stack](project_stack.md) — Python 3.10 + dataclasses
```

## Agent 集成

### 构造函数

```python
class Agent:
    def __init__(self, ..., auto_memory: AutoMemory | None = None):
        self.auto_memory = auto_memory
```

### 对话结束时提取

在 `_run_tool_loop()` 和 `stream()` 中，当 LLM 最终回复（无工具调用）时触发：

```python
# 如果没有工具调用，返回结果
if not response.has_tool_use:
    self._maybe_extract_memories()  # 提取持久性记忆
    return response
```

`_maybe_extract_memories()` 静默执行，失败不影响主流程：

```python
def _maybe_extract_memories(self) -> None:
    if not self.auto_memory:
        return
    try:
        self.auto_memory.extract_and_save(self.messages)
    except Exception:
        pass
```

### build_agent 集成

在 `__main__.py` 中创建 AutoMemory 并注入记忆到 system prompt：

```python
# Auto-Memory（跨会话记忆）
auto_memory = AutoMemory(llm=llm, cwd=config.cwd)
memory_prompt = auto_memory.load_memory_prompt()
if memory_prompt:
    system_prompt = system_prompt + "\n\n" + memory_prompt if system_prompt else memory_prompt

agent = Agent(..., auto_memory=auto_memory)
```

新会话启动时，如果项目有已有记忆，`MEMORY.md` 的内容会被注入到 system prompt 中，LLM 自动获得历史上下文。

## REPL 命令

`/memory` 命令查看当前项目的所有记忆：

```
[1] 你: /memory

--- 跨会话记忆 (~/.coding-agent/projects/a1b2c3d4e5f6/memory) ---
  [user] User Preferences — User coding preferences
  [project] Tech Stack — Python 3.10 + dataclasses
  [feedback] Testing Feedback — Always run pytest before commit

  共 3 条记忆
```

## 数据流总结

```
会话启动
  ├→ AutoMemory.load_memory_prompt()
  │    └→ 读取 MEMORY.md → 注入 system prompt
  ↓
对话进行中...
  ↓
LLM 最终回复（无工具调用）
  ├→ _maybe_extract_memories()
  │    ├→ 扫描已有记忆（去重）
  │    ├→ 构建对话摘要
  │    ├→ LLM 提取 JSON 格式记忆
  │    ├→ 保存 .md 文件
  │    └→ 重建 MEMORY.md 索引
  ↓
下次新会话
  └→ MEMORY.md 已更新，LLM 获得历史上下文
```

## 与 Claude Code 的差异

| 方面 | Claude Code | 我们的实现 |
|---|---|---|
| 提取方式 | forked sub-agent + 完整工具集 | 直接 llm.chat()（简化） |
| 触发时机 | 每次 query loop 结束 | 同样，LLM 最终回复时 |
| 索引格式 | MEMORY.md（手动维护） | MEMORY.md（自动重建） |
| 记忆类型 | 4 种 + team memory | 4 种（无 team） |
| 记忆管理 | /memory 命令（完整 CRUD） | /memory 命令（查看） |

## 小结

Auto-Memory 实现了跨会话的"学习"能力：

- **自动提取**：对话结束时静默提取，用户不感知
- **持久存储**：每条记忆一个 md 文件，frontmatter 标注类型
- **自动加载**：新会话启动时注入 system prompt
- **去重检查**：提取时传入已有记忆清单，避免重复
- **REPL 查看**：`/memory` 命令随时查看记忆

下一篇我们将实现 **MCP Client（Model Context Protocol）**——让 Agent 连接外部工具服务器，动态发现和调用工具，大幅扩展 Agent 的能力边界。
