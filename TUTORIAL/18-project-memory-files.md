# 从零实现 Coding Agent（十八）：项目记忆文件

前面十七篇实现了完整的 Agent 核心循环。但有一个问题：**Agent 对项目的理解全靠用户每次对话时手动描述**。

项目用什么语言？什么框架？代码风格是 camelCase 还是 snake_case？测试用 pytest 还是 unittest？这些信息每次都要重复，或者 Agent 猜错了再纠正。

本篇实现 **分层项目记忆文件系统**：从多个位置自动加载 AGENTS.md 及规则文件，让 Agent 启动时就"知道"项目约定。

这对应 Claude Code 源码中的 `utils/claudemd.ts` + `context.ts`。

---

## 之前的问题

之前的 `context.py` 只做了一件事：从 cwd 向上查找单个 `AGENTS.md`。

```python
def find_agents_md(start_dir):
    current = Path(start_dir).resolve()
    while True:
        agents_file = current / "AGENTS.md"
        if agents_file.exists():
            return str(agents_file)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None
```

这有几个局限：

1. **只能有一个文件** —— 所有规范塞进一个 AGENTS.md，维护困难
2. **没有个人偏好** —— 团队共享的 AGENTS.md 不适合写个人习惯（如"用中文回复"）
3. **没有本地覆盖** —— 想在本地加点规则但不想提交到 git，没有办法
4. **不能引用其他文件** —— 不能把编码规范拆成多个文件组织

---

## 分层记忆文件设计

参考 Claude Code 的设计，我们实现三层记忆文件：

```
优先级（低 → 高）:

~/.coding-agent/AGENTS.md          # 用户级：个人全局偏好
~/.coding-agent/rules/*.md         # 用户级：个人全局规则

/project/AGENTS.md            # 项目级：团队共享的项目规范
/project/.coding-agent/AGENTS.md   # 项目级：.coding-agent 目录下的规范
/project/.coding-agent/rules/*.md  # 项目级：规则目录（多文件）

/project/AGENTS.local.md      # 本地级：个人项目配置（gitignore）
```

### 为什么分三层？

**用户级**（`~/.coding-agent/`）：跨项目的个人偏好。比如"所有回复使用中文"、"不要在代码中加注释"。不管在哪个项目，都生效。

**项目级**（项目目录）：团队共享的编码规范，提交到 git。多个文件可以分主题组织：

```
.coding-agent/
├── AGENTS.md           # 总纲
└── rules/
    ├── 01-style.md     # 代码风格
    ├── 02-testing.md   # 测试规范
    └── 03-api.md       # API 设计规范
```

**本地级**（`AGENTS.local.md`）：个人的项目级配置，不提交到 git。比如"这个项目我负责后端，重点关注 API 层"。

### 优先级与加载顺序

Claude Code 的设计：**后加载的优先级更高**。LLM 对靠后的内容给予更多注意。

所以加载顺序是：用户级 → 项目级（从根到 cwd）→ 本地级。本地配置最后加载，优先级最高。

---

## 实现

### MemoryFile 数据类

```python
@dataclass
class MemoryFile:
    """一个已加载的记忆文件。"""
    path: str           # 文件绝对路径
    content: str        # 文件内容
    memory_type: str    # User / Project / Local
    source: str         # 友好显示路径（如 ~/.coding-agent/AGENTS.md）
```

### 核心函数：load_memory_files()

```python
def load_memory_files(cwd: str = ".") -> list[MemoryFile]:
    result = []
    processed = set()  # 防止重复加载

    # 1. 用户级
    user_dir = Path.home() / ".coding-agent"
    result.extend(_process_memory_file(str(user_dir / "AGENTS.md"), "User", processed))
    result.extend(_load_rules_dir(str(user_dir / "rules"), "User", processed))

    # 2. 项目级（从根目录向 cwd 遍历）
    abs_cwd = Path(cwd).resolve()
    dirs = []
    current = abs_cwd
    while True:
        dirs.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    for d in reversed(dirs):  # 从根到 cwd
        result.extend(_process_memory_file(str(d / "AGENTS.md"), "Project", processed))
        result.extend(_process_memory_file(str(d / ".coding-agent" / "AGENTS.md"), "Project", processed))
        result.extend(_load_rules_dir(str(d / ".coding-agent" / "rules"), "Project", processed))

    # 3. 本地级（从根到 cwd 每层目录查找 AGENTS.local.md）
    # 注：Claude Code 在每个目录层级都会加载 CLAUDE.local.md，
    # 我们的教学实现简化为只找最近的一个
    current = abs_cwd
    while True:
        local_md = current / "AGENTS.local.md"
        if local_md.exists():
            result.extend(_process_memory_file(str(local_md), "Local", processed))
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    return result
```

关键设计：

1. **`processed` 集合** —— 防止同一文件被多次加载（可能通过 @include 和直接发现重复）
2. **从根到 cwd** —— `reversed(dirs)` 确保越靠近 cwd 的文件越后加载，优先级越高
3. **只找最近的 AGENTS.local.md** —— 不像项目级那样每层都加载

### @include 指令

记忆文件中可以用 `@path` 引用其他文件：

```markdown
# 项目规范

使用 Python 3.12+，代码风格遵循 PEP 8。

@./coding-standards.md
@./api-guidelines.md
```

实现逻辑：

```python
_INCLUDE_PATTERN = re.compile(r"(?:^|\s)@((?:\./|~/|/)[^\s]+)", re.MULTILINE)

def _extract_include_paths(content, base_dir):
    text_only = _strip_code_blocks(content)  # 排除代码块
    paths = []
    for match in _INCLUDE_PATTERN.finditer(text_only):
        raw = match.group(1)
        resolved = _resolve_include_path(raw, base_dir)
        if resolved:
            paths.append(resolved)
    return paths
```

`_process_memory_file()` 递归处理 @include：

```python
def _process_memory_file(file_path, memory_type, processed, depth=0):
    if normalized in processed or depth >= MAX_INCLUDE_DEPTH:
        return []

    processed.add(normalized)
    content = _read_file_safe(file_path)
    if not content:
        return []

    result = []

    # 主文件排在前面
    result.append(MemoryFile(path=file_path, content=content, ...))

    # 再处理 @include（被引用的文件排在后面，优先级更低）
    include_paths = _extract_include_paths(content, base_dir)
    for inc_path in include_paths:
        result.extend(_process_memory_file(inc_path, memory_type, processed, depth + 1))

    return result
```

注意 include 的文件排在引用它的文件**后面**——Claude Code 的设计是父文件优先于子文件（先加载的内容在 prompt 中更靠前）。

安全措施：
- **最大深度 5** —— 防止无限递归
- **`processed` 集合** —— 防止循环引用（A include B，B include A）
- **代码块排除** —— `_strip_code_blocks()` 移除 ``` 包裹的内容，避免把代码示例中的 `@path` 当作指令
- **扩展名白名单** —— 只允许 include 文本文件（.md、.py、.json 等），不会加载二进制文件

### rules 目录

```python
def _load_rules_dir(rules_dir, memory_type, processed):
    rules_path = Path(rules_dir)
    if not rules_path.is_dir():
        return []

    result = []
    md_files = sorted(rules_path.glob("*.md"))  # 按名称排序
    for md_file in md_files:
        result.extend(_process_memory_file(str(md_file), memory_type, processed))
    return result
```

`sorted()` 保证确定性顺序。推荐的文件命名：`01-style.md`、`02-testing.md`，用数字前缀控制加载顺序。

### 系统提示组装

`build_system_prompt()` 将所有记忆文件组装到 system prompt：

```python
def build_system_prompt(base_system=None, cwd=".", include_date=True):
    parts = []

    if base_system:
        parts.append(base_system)

    memory_files = load_memory_files(cwd)
    if memory_files:
        memory_parts = []
        total_chars = 0

        for mf in memory_files:
            remaining = MAX_MEMORY_CHARACTERS - total_chars
            if remaining <= 0:
                break
            content = mf.content[:remaining]
            total_chars += len(content)
            label = f"[{mf.memory_type}] {mf.source}"
            memory_parts.append(f"### {label}\n\n{content}")

        parts.append(
            "## 项目规范\n\n"
            "以下是项目的编码规范和指令，请严格遵守。\n\n"
            + "\n\n".join(memory_parts)
        )

    # 上下文信息（日期、工作目录）...
    return "\n\n".join(parts)
```

`MAX_MEMORY_CHARACTERS = 40000` 是截断保护。记忆文件可能很多，不能无限制地塞进 system prompt。40000 字符大约 10000-13000 tokens，是合理的上限。

---

## 与 Claude Code 的对比

| 维度 | Claude Code | 我们的实现 |
|------|-------------|------------|
| 记忆层级 | Managed + User + Project + Local | User + Project + Local |
| 文件名 | CLAUDE.md | AGENTS.md |
| 规则目录 | .claude/rules/*.md | .coding-agent/rules/*.md |
| @include | 支持 @path、@./path、@~/path、@/path | 同上 |
| 条件规则 | frontmatter 中指定匹配路径 | 不支持（MVP 简化） |
| 字符上限 | 40000 | 40000 |
| HTML 注释 | 支持 `<!-- -->` 剥离 | 不支持（简化） |
| 符号链接 | 安全解析 | 不支持（简化） |

我们的简化：
- **没有 Managed 级别** —— 那是企业部署场景，个人使用不需要
- **没有条件规则** —— Claude Code 支持 frontmatter 中指定"只在匹配特定路径时加载"，MVP 不需要
- **同步加载** —— Claude Code 用异步文件 IO，我们用同步（文件都很小，不需要异步）

---

## 使用示例

### 全局个人偏好

```bash
mkdir -p ~/.coding-agent
cat > ~/.coding-agent/AGENTS.md << 'EOF'
# 个人偏好

- 使用中文回复
- 代码注释用英文
- 优先使用函数式编程风格
EOF
```

### 项目规范

```bash
cat > AGENTS.md << 'EOF'
# 项目规范

Python 3.12+ 项目，使用 ruff 格式化。

测试用 pytest，覆盖率要求 > 80%。

@./docs/coding-standards.md
EOF
```

### 规则目录

```bash
mkdir -p .coding-agent/rules

cat > .coding-agent/rules/01-style.md << 'EOF'
# 代码风格
- snake_case 命名
- 行宽 88 字符
- 使用 type hints
EOF

cat > .coding-agent/rules/02-testing.md << 'EOF'
# 测试规范
- 每个模块配套 test_ 文件
- 使用 pytest fixtures
- mock 外部依赖
EOF
```

### 本地覆盖

```bash
# 这个文件不提交到 git
echo "AGENTS.local.md" >> .gitignore

cat > AGENTS.local.md << 'EOF'
# 本地配置
我是后端开发，重点关注 API 层。
调试时优先用 print 而不是 debugger。
EOF
```

### 启动效果

```
$ python -m agent "帮我优化这个函数"
[anthropic / claude-sonnet-4-20250514] 工作目录: /Users/xxx/myproject
  记忆文件: 已加载 5 个
    [User] ~/.coding-agent/AGENTS.md
    [Project] /Users/xxx/myproject/AGENTS.md
    [Project] /Users/xxx/myproject/.coding-agent/rules/01-style.md
    [Project] /Users/xxx/myproject/.coding-agent/rules/02-testing.md
    [Local] /Users/xxx/myproject/AGENTS.local.md
```

---

## 小结

这一篇重写了 `context.py`，实现分层项目记忆文件系统：

1. **三层记忆** —— User（个人全局）→ Project（团队共享）→ Local（个人项目）
2. **规则目录** —— `.coding-agent/rules/*.md` 支持多文件组织，按文件名排序
3. **@include** —— 引用其他文件，递归展开，防循环，代码块内不解析
4. **截断保护** —— 40000 字符上限，防止 system prompt 过大
5. **向后兼容** —— `get_context_info()` 保留旧接口字段

核心理念：**项目规范应该是"写一次，永久生效"的**。团队规范提交到 git，个人偏好放在 `~/.coding-agent/`，本地覆盖放在 `AGENTS.local.md`。Agent 启动时自动加载，不需要每次对话重复描述。

下一篇将实现 Plan Mode——让 Agent 对复杂任务先规划再动手。
