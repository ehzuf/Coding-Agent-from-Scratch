# 从零实现 Coding Agent（五）：文件操作工具组

前四篇文章我们实现了 LLM 调用、流式输出、多轮对话和 Tool Use 协议。现在，让我们添加一组真正"干活"的工具——文件操作。

这三篇文章将一起介绍：
- Bash 工具——执行 shell 命令
- 文件读写工具——读取和写入文件
- 文件编辑 + Glob + Grep——精确编辑、路径匹配、内容搜索

## 为什么需要这些工具？

Coding agent 的核心任务是**修改代码**。要修改代码，你需要：

1. **查看**现有代码（Read）
2. **搜索**特定内容（Grep）
3. **定位**文件位置（Glob）
4. **修改**代码（Edit）
5. **执行**命令测试（Bash）

这些工具构成了一套完整的文件操作能力。

## Bash 工具

Bash 工具让 agent 能够执行 shell 命令。这是最直接、最强大的工具，也是风险最高的。

### 实现

```python
# agent/tools/bash.py

import subprocess
import os
from typing import Any
from .base import Tool

class BashTool(Tool):
    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return """执行 shell 命令。
命令会在 shell 中执行（bash -c），支持管道、重定向等 shell 特性。
默认超时为 120 秒。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "timeout": {
                    "type": "number",
                    "description": "超时时间（毫秒），默认 120000",
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录，默认为当前目录",
                },
            },
            "required": ["command"],
        }

    def call(self, input: dict[str, Any]) -> str:
        command = input.get("command", "")
        timeout = input.get("timeout", 120000)
        cwd = input.get("cwd")

        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout / 1000,  # 转换为秒
            cwd=cwd,
        )

        return f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}\n\nexit_code: {result.returncode}"
```

### 关键点

- **`capture_output=True`**：捕获 stdout 和 stderr
- **`timeout`**：防止命令无限运行（注意：参数单位为毫秒，与 Claude Code 一致。内部传给 `subprocess.run` 时需转换为秒）
- **`cwd`**：支持在指定目录执行

### 使用示例

```
用户: 查看当前目录
Agent: [bash] ls -la

用户: 运行测试
Agent: [bash] python -m pytest

用户: 查看 git 状态
Agent: [bash] git status
```

## 文件读写工具

### Read 工具

```python
# agent/tools/read.py

import os
from typing import Any
from .base import Tool

class ReadTool(Tool):
    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return """读取文件内容。
支持指定偏移量和行数限制，用于读取大文件的部分内容。
返回的内容带行号。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要读取的文件路径",
                },
                "offset": {
                    "type": "integer",
                    "description": "从第几行开始读取（默认 1，即第一行）",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取多少行",
                },
            },
            "required": ["file_path"],
        }

    def call(self, input: dict[str, Any]) -> str:
        file_path = input.get("file_path", "")
        offset = input.get("offset", 1)
        limit = input.get("limit")

        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if offset > 1:
            lines = lines[offset - 1:]
        if limit:
            lines = lines[:limit]

        # 添加行号
        result = []
        for i, line in enumerate(lines, offset):
            result.append(f"{i:6}\t{line.rstrip()}")

        return "\n".join(result)
```

### Write 工具

```python
# agent/tools/write.py

import os
from typing import Any
from .base import Tool

class WriteTool(Tool):
    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return """写入或覆盖文件。
如果文件不存在会创建，如果存在会覆盖。
会自动创建所需的父目录。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要写入的文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的内容",
                },
            },
            "required": ["file_path", "content"],
        }

    def call(self, input: dict[str, Any]) -> str:
        file_path = input.get("file_path", "")
        content = input.get("content", "")

        # 确保父目录存在
        parent_dir = os.path.dirname(file_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        return f"成功写入文件: {file_path}"
```

### 设计要点

1. **Read 带行号**：方便 LLM 引用特定行
2. **Read 支持 offset/limit**：处理大文件时避免超出上下文窗口（offset 从 1 开始，1-based 行号）
3. **Write 自动创建目录**：减少一步 mkdir 操作
4. **Write 需要"先读后写"验证**：Claude Code 要求对已存在的文件先调用 Read 才能 Write，防止 LLM 在未阅读文件内容的情况下覆盖已有文件。我们在教学实现中省略了这一验证，但生产环境建议添加

## 文件编辑 + Glob + Grep

### Edit 工具

这是最重要的工具之一。与 Write 不同，Edit 只修改文件的特定部分，而不是重写整个文件。

```python
# agent/tools/edit.py

import os
from typing import Any
from .base import Tool

class EditTool(Tool):
    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return """通过精确字符串替换来编辑文件。
在文件中查找 old_string，并将其替换为 new_string。
这是比重写整个文件更精确的编辑方式。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配项",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    def call(self, input: dict[str, Any]) -> str:
        file_path = input.get("file_path", "")
        old_string = input.get("old_string", "")
        new_string = input.get("new_string", "")
        replace_all = input.get("replace_all", False)

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        if old_string not in content:
            return "错误：未找到要替换的字符串"

        count = content.count(old_string)
        if count > 1 and not replace_all:
            return f"错误：找到 {count} 处匹配，请设置 replace_all=true"

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return f"成功编辑文件: {file_path}"
```

### 为什么选择字符串替换而非行号？

行号的问题：
1. 文件修改后行号会变化
2. 插入/删除行后，后续行号全部偏移
3. 难以定位跨越多行的代码块

字符串替换的优势：
1. **上下文感知**：替换的内容本身就是定位依据
2. **多行支持**：可以匹配包含换行的代码块
3. **稳定性**：只要代码逻辑不变，就能正确定位

### Glob 工具

```python
# agent/tools/glob.py

import glob as glob_module
import os
from typing import Any
from .base import Tool

class GlobTool(Tool):
    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return """使用 glob 模式匹配文件路径。
支持 ** 递归匹配，返回按修改时间排序的文件列表。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob 模式，如 **/*.py",
                },
                "path": {
                    "type": "string",
                    "description": "搜索的根目录",
                },
            },
            "required": ["pattern"],
        }

    def call(self, input: dict[str, Any]) -> str:
        pattern = input.get("pattern", "")
        path = input.get("path", ".")

        full_pattern = os.path.join(path, pattern)
        matches = glob_module.glob(full_pattern, recursive=True)
        files = [f for f in matches if os.path.isfile(f)]

        # 按修改时间排序
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)

        return "\n".join(files)
```

### Grep 工具

```python
# agent/tools/grep.py

import os
import re
from typing import Any
from .base import Tool

class GrepTool(Tool):
    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return """在文件中搜索正则表达式。
返回匹配的行及其行号，格式：file_path:line: content"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "文件或目录"},
                "include": {"type": "string", "description": "文件过滤，如 *.py"},
            },
            "required": ["pattern"],
        }

    def call(self, input: dict[str, Any]) -> str:
        pattern = input.get("pattern", "")
        path = input.get("path", ".")
        include = input.get("include")

        regex = re.compile(pattern)

        # 收集要搜索的文件
        files_to_search = []
        if os.path.isfile(path):
            files_to_search.append(path)
        else:
            for root, dirs, files in os.walk(path):
                for f in files:
                    if include and not f.endswith(include.removeprefix("*.")):
                        continue
                    files_to_search.append(os.path.join(root, f))

        # 搜索
        matches = []
        for file_path in files_to_search:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line_num, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append(f"{file_path}:{line_num}: {line.rstrip()}")

        return "\n".join(matches)
```

## 工具组合使用示例

这些工具可以组合使用，形成完整的工作流：

```
用户: 找到所有 Python 文件中包含 "TODO" 的地方
Agent: [glob] pattern="**/*.py"
       [grep] pattern="TODO", path=".", include="*.py"

用户: 查看 agent/tools/base.py 的内容
Agent: [read] file_path="agent/tools/base.py"

用户: 在 base.py 中添加一个新方法
Agent: [read] file_path="agent/tools/base.py"
       [edit] file_path="agent/tools/base.py",
              old_string="...",
              new_string="..."

用户: 运行测试确保修改正确
Agent: [bash] command="python -m pytest tests/"
```

## 安全考虑

这些工具功能强大，但也存在风险：

| 工具 | 风险 | 缓解措施 |
|------|------|----------|
| Bash | 任意命令执行 | 后续添加权限系统，限制可执行命令 |
| Write | 覆盖重要文件 | 权限控制 + 备份机制 |
| Edit | 意外修改 | 要求精确匹配，减少误操作 |

后续文章会实现**权限系统**，允许用户配置哪些工具可以执行、哪些命令允许运行。

## 这一步我们学到了什么

1. **文件操作是 coding agent 的核心**：Read/Write/Edit 构成完整的代码修改能力
2. **字符串替换优于行号**：更稳定、更精确
3. **Glob + Grep 是定位工具**：快速找到需要修改的文件和位置
4. **Bash 是万能工具**：但也是最危险的，需要权限控制

下一篇文章，我们将实现 **System Prompt + AGENTS.md**——从工作目录读取配置文件，注入到对话上下文中，让 agent 了解项目背景和编码规范。
