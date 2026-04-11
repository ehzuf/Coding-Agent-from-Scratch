# 从 Coding Agent 到个人助理（一）：上下文引用系统

前面的系列中，我们实现了一个完整的 Coding Agent——它能读写文件、搜索代码、执行命令。但有一个交互效率问题始终存在：当用户想让 Agent 看某个文件、一段 diff、或一个网页时，只能手动复制粘贴内容，或者等 Agent 自己去找。

本篇实现 **上下文引用系统**——让用户在消息中通过 `@` 语法直接引用外部内容，Agent 自动展开并注入上下文。

## 为什么需要上下文引用

假设用户说：

> 帮我 review 一下 agent/config.py 这个文件

Agent 会先调用 `read` 工具把文件内容读进来，然后再分析。这个过程需要一轮工具调用，而且用户无法控制注入的内容范围。

如果用户可以这样写：

> 帮我 review 一下 @file:agent/config.py

Agent 在收到消息之前，系统就自动把文件内容展开并附加到消息里。**省去一轮工具调用，用户还能精确控制注入什么**。

更进一步，我们可以支持多种引用类型：

```
@file:agent/config.py        读取文件
@file:agent/config.py:10-50  读取文件的第 10-50 行
@folder:agent/tools           列出目录结构
@diff                         当前 git diff
@staged                       已暂存的变更
@git:3                        最近 3 次 commit 的详细 diff
@url:https://example.com      抓取网页内容
```

这就是 Hermes Agent 中的 Context References 系统。我们来看看怎么实现它。

## 整体设计

上下文引用的处理流程分为四步：

```
用户消息 → 正则解析 → 引用展开 → 预算检查 → 拼接结果
```

1. **正则解析**：扫描用户消息，提取所有 `@xxx` 引用
2. **引用展开**：根据类型（file/folder/diff/url...）获取实际内容
3. **预算检查**：注入内容不能超过上下文窗口的一半，否则拒绝
4. **拼接结果**：把原消息中的 `@xxx` 标记移除，附加展开后的内容

关键数据结构：

```python
# agent/context_references.py

from dataclasses import dataclass, field

@dataclass(frozen=True)
class ContextReference:
    """一个解析出来的引用"""
    raw: str              # 原始文本，如 "@file:agent/config.py"
    kind: str             # 引用类型："file", "folder", "diff", "staged", "git", "url"
    target: str           # 目标路径或 URL
    start: int            # 在原消息中的起始位置
    end: int              # 在原消息中的结束位置
    line_start: int | None = None  # 行范围（仅 file 类型）
    line_end: int | None = None


@dataclass
class ContextReferenceResult:
    """引用处理的完整结果"""
    message: str                                    # 处理后的消息（引用标记移除 + 内容附加）
    original_message: str                           # 原始消息
    references: list[ContextReference] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    injected_tokens: int = 0                        # 注入了多少 token
    expanded: bool = False                          # 是否有内容被展开
    blocked: bool = False                           # 是否因超预算被拒绝
```

`ContextReference` 用 `frozen=True`，因为解析结果不应被修改。`ContextReferenceResult` 是可变的，需要在处理过程中逐步填充。

## 正则解析

第一步是从用户消息中提取所有 `@` 引用。设计一条正则覆盖所有类型：

```python
import re

REFERENCE_PATTERN = re.compile(
    r"(?<![\w/])@(?:(?P<simple>diff|staged)\b|(?P<kind>file|folder|git|url):(?P<value>\S+))"
)
```

拆解一下这条正则：

- `(?<![\w/])` —— 负向后行断言，确保 `@` 前面不是字母、数字或 `/`。这防止把邮箱地址 `user@example.com` 或路径 `/usr/@cache` 误识别为引用
- `@` —— 引用前缀
- `(?:...|...)` —— 两种模式任选其一：
  - `(?P<simple>diff|staged)\b` —— 简单类型，不带参数（`@diff`、`@staged`）
  - `(?P<kind>file|folder|git|url):(?P<value>\S+)` —— 带参数类型，`@kind:value` 格式

解析函数：

```python
def parse_context_references(message: str) -> list[ContextReference]:
    """从消息中提取所有 @ 引用"""
    refs = []
    if not message:
        return refs

    for match in REFERENCE_PATTERN.finditer(message):
        # 简单类型：@diff, @staged
        simple = match.group("simple")
        if simple:
            refs.append(ContextReference(
                raw=match.group(0), kind=simple, target="",
                start=match.start(), end=match.end(),
            ))
            continue

        # 带参数类型：@file:path, @url:https://...
        kind = match.group("kind")
        value = match.group("value") or ""
        # 清理尾部标点（用户可能写 "看看@file:a.py，然后..."）
        value = value.rstrip(",.;!?")
        
        line_start = line_end = None
        target = value

        # @file 支持行范围：@file:path:10-50
        if kind == "file":
            range_match = re.match(
                r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$", value
            )
            if range_match:
                target = range_match.group("path")
                line_start = int(range_match.group("start"))
                line_end = int(range_match.group("end") or range_match.group("start"))

        refs.append(ContextReference(
            raw=match.group(0), kind=kind, target=target,
            start=match.start(), end=match.end(),
            line_start=line_start, line_end=line_end,
        ))

    return refs
```

注意 `@file:path:10-50` 的处理——冒号既用于分隔 `kind:value`，又用于分隔 `path:lines`。正则先匹配出完整的 value（`path:10-50`），然后在 file 类型内部再做一次解析，把行范围提取出来。

试一下：

```python
refs = parse_context_references("帮我看看 @file:agent/config.py:10-50 和 @diff")
# refs[0]: kind="file", target="agent/config.py", line_start=10, line_end=50
# refs[1]: kind="diff", target=""
```

## 引用展开

解析出引用后，下一步是根据类型获取实际内容。每种类型对应一个展开函数：

```python
import os
import subprocess
from pathlib import Path

def _expand_reference(ref, cwd):
    """根据引用类型展开内容，返回 (warning, block)"""
    try:
        if ref.kind == "file":
            return _expand_file(ref, cwd)
        elif ref.kind == "folder":
            return _expand_folder(ref, cwd)
        elif ref.kind == "diff":
            return _expand_git(ref, cwd, ["diff"], "git diff")
        elif ref.kind == "staged":
            return _expand_git(ref, cwd, ["diff", "--staged"], "git diff --staged")
        elif ref.kind == "git":
            count = max(1, min(int(ref.target or "1"), 10))  # 限制 1-10
            return _expand_git(ref, cwd, ["log", f"-{count}", "-p"], f"git log -{count} -p")
        elif ref.kind == "url":
            return _expand_url(ref)
    except Exception as e:
        return f"{ref.raw}: {e}", None

    return f"{ref.raw}: unsupported reference type", None
```

每个展开函数返回一个元组 `(warning, block)`——如果展开成功，`block` 是格式化后的内容字符串；如果失败，`warning` 是错误信息。

### 文件展开

```python
def _expand_file(ref, cwd):
    """展开 @file 引用"""
    path = _resolve_path(cwd, ref.target)

    if not path.exists():
        return f"{ref.raw}: file not found", None
    if not path.is_file():
        return f"{ref.raw}: not a file", None

    text = path.read_text(encoding="utf-8")

    # 支持行范围
    if ref.line_start is not None:
        lines = text.splitlines()
        start = max(ref.line_start - 1, 0)
        end = min(ref.line_end or ref.line_start, len(lines))
        text = "\n".join(lines[start:end])

    lang = _detect_language(path)
    tokens = _estimate_tokens(text)
    return None, f"📄 {ref.raw} ({tokens} tokens)\n```{lang}\n{text}\n```"


def _resolve_path(cwd, target):
    """解析路径，相对路径基于 cwd"""
    path = Path(os.path.expanduser(target))
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _detect_language(path):
    """根据后缀猜测代码语言"""
    mapping = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".json": "json", ".md": "markdown", ".sh": "bash",
        ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    }
    return mapping.get(path.suffix.lower(), "")
```

展开结果带有 emoji 标记和 token 估算，方便用户和 Agent 理解注入了多少内容。

### Git 引用展开

`@diff`、`@staged`、`@git:N` 三种引用都走同一个函数，只是 git 命令参数不同：

```python
def _expand_git(ref, cwd, args, label):
    """展开 git 相关引用"""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return f"{ref.raw}: git command timed out", None

    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "git command failed"
        return f"{ref.raw}: {stderr}", None

    content = result.stdout.strip()
    if not content:
        content = "(no output)"

    tokens = _estimate_tokens(content)
    return None, f"🧾 {label} ({tokens} tokens)\n```diff\n{content}\n```"
```

注意 timeout 的设置——git 操作在大仓库上可能很慢，30 秒是合理的上限。

### 目录引用展开

```python
def _expand_folder(ref, cwd):
    """展开 @folder 引用，生成目录树"""
    path = _resolve_path(cwd, ref.target)

    if not path.exists():
        return f"{ref.raw}: folder not found", None
    if not path.is_dir():
        return f"{ref.raw}: not a folder", None

    listing = _build_folder_listing(path, max_items=200)
    tokens = _estimate_tokens(listing)
    return None, f"📁 {ref.raw} ({tokens} tokens)\n{listing}"


def _build_folder_listing(path, max_items=200):
    """构建目录树，限制最大条目数"""
    lines = [f"{path.name}/"]
    count = 0

    for root, dirs, files in os.walk(path):
        # 跳过隐藏目录和缓存
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        files = sorted(f for f in files if not f.startswith("."))

        depth = Path(root).relative_to(path)
        indent = "  " * len(depth.parts)

        for d in dirs:
            lines.append(f"{indent}- {d}/")
            count += 1
            if count >= max_items:
                lines.append("- ...")
                return "\n".join(lines)

        for f in files:
            lines.append(f"{indent}- {f}")
            count += 1
            if count >= max_items:
                lines.append("- ...")
                return "\n".join(lines)

    return "\n".join(lines)
```

`max_items=200` 是防御性设计——大项目的目录可能有几万个文件，全部列出会直接撑爆上下文。

### URL 引用展开

URL 展开相对简单，但涉及外部 HTTP 请求。为了保持教学版的简洁，我们用 Python 标准库：

```python
import urllib.request
import json

def _expand_url(ref):
    """展开 @url 引用，抓取网页文本内容"""
    url = ref.target
    if not url.startswith(("http://", "https://")):
        return f"{ref.raw}: invalid URL", None

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"{ref.raw}: failed to fetch ({e})", None

    # 简单提取文本：去掉 HTML 标签
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", " ", text).strip()

    # 截断过长内容
    if len(text) > 50000:
        text = text[:50000] + "\n... (truncated)"

    tokens = _estimate_tokens(text)
    return None, f"🌐 {ref.raw} ({tokens} tokens)\n{text}"
```

实际生产环境中，Hermes 用 LLM 辅助提取网页结构化内容（类似 Readability 算法），效果更好。这里为教学简化，直接去标签取纯文本。

## 上下文预算控制

引用展开后可能注入大量内容。如果用户写了 `@folder:./` 加上 `@file:一个超大文件`，注入量可能远超上下文窗口。

Hermes 的方案是**双层预算限制**：

- **软限制（25%）**：超过时发出警告，但仍然注入
- **硬限制（50%）**：超过时拒绝注入，返回错误

```python
def _estimate_tokens(text):
    """粗略估算 token 数（1 token ≈ 4 个字符）"""
    return max(1, len(text) // 4)


def preprocess_context_references(message, *, cwd, context_length):
    """预处理用户消息中的 @ 引用
    
    Args:
        message: 用户原始消息
        cwd: 当前工作目录
        context_length: 模型上下文窗口大小（token 数）
    """
    refs = parse_context_references(message)
    if not refs:
        return ContextReferenceResult(message=message, original_message=message)

    cwd_path = Path(cwd).resolve()
    warnings = []
    blocks = []
    injected_tokens = 0

    # 逐个展开引用
    for ref in refs:
        warning, block = _expand_reference(ref, cwd_path)
        if warning:
            warnings.append(warning)
        if block:
            blocks.append(block)
            injected_tokens += _estimate_tokens(block)

    # 预算检查
    hard_limit = max(1, int(context_length * 0.50))
    soft_limit = max(1, int(context_length * 0.25))

    if injected_tokens > hard_limit:
        warnings.append(
            f"Context injection refused: {injected_tokens} tokens "
            f"exceeds the 50% hard limit ({hard_limit})."
        )
        return ContextReferenceResult(
            message=message, original_message=message,
            references=refs, warnings=warnings,
            injected_tokens=injected_tokens,
            expanded=False, blocked=True,
        )

    if injected_tokens > soft_limit:
        warnings.append(
            f"Context injection warning: {injected_tokens} tokens "
            f"exceeds the 25% soft limit ({soft_limit})."
        )

    # 从原消息中移除 @ 标记
    stripped = _remove_reference_tokens(message, refs)

    # 拼接最终消息
    final = stripped
    if warnings:
        final += "\n\n--- Context Warnings ---\n"
        final += "\n".join(f"- {w}" for w in warnings)
    if blocks:
        final += "\n\n--- Attached Context ---\n\n"
        final += "\n\n".join(blocks)

    return ContextReferenceResult(
        message=final.strip(), original_message=message,
        references=refs, warnings=warnings,
        injected_tokens=injected_tokens,
        expanded=bool(blocks or warnings), blocked=False,
    )


def _remove_reference_tokens(message, refs):
    """从消息中移除 @ 引用标记"""
    pieces = []
    cursor = 0
    for ref in refs:
        pieces.append(message[cursor:ref.start])
        cursor = ref.end
    pieces.append(message[cursor:])
    text = "".join(pieces)
    # 清理多余空白
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()
```

为什么是 50% 硬限制？因为上下文窗口还需要留空间给：system prompt、消息历史、工具定义、以及 Agent 的输出。如果引用就吃掉了大半，Agent 几乎无法正常工作。

## 安全防护

允许用户通过 `@file` 读取文件引入了安全风险——如果用户引用了 `.ssh/id_rsa` 或 `.aws/credentials`，这些敏感内容会直接发送给 LLM API。

Hermes 的做法是维护一个敏感路径黑名单：

```python
_SENSITIVE_DIRS = (".ssh", ".aws", ".gnupg", ".kube", ".docker")
_SENSITIVE_FILES = (
    ".ssh/id_rsa", ".ssh/id_ed25519", ".ssh/config",
    ".ssh/authorized_keys", ".netrc", ".pgpass", ".npmrc", ".pypirc",
)


def _check_path_safety(path):
    """检查路径是否安全，不安全则抛异常"""
    home = Path.home().resolve()

    # 检查敏感目录
    for sensitive_dir in _SENSITIVE_DIRS:
        sensitive_path = home / sensitive_dir
        try:
            path.relative_to(sensitive_path)
            raise ValueError(f"path is inside sensitive directory: {sensitive_dir}")
        except ValueError as e:
            if "sensitive directory" in str(e):
                raise
            continue  # 不在这个目录下，继续检查

    # 检查敏感文件
    for sensitive_file in _SENSITIVE_FILES:
        if path == (home / sensitive_file).resolve():
            raise ValueError(f"path is a sensitive file: {sensitive_file}")
```

在 `_expand_file` 和 `_expand_folder` 的开头调用这个检查：

```python
def _expand_file(ref, cwd):
    path = _resolve_path(cwd, ref.target)
    _check_path_safety(path)  # 安全检查
    # ... 后续逻辑
```

此外，还需要防止**目录遍历攻击**。用户如果写 `@file:../../etc/passwd`，解析出的路径可能跳出工作目录。解决方法是加一个 `allowed_root` 参数：

```python
def _resolve_path(cwd, target, *, allowed_root=None):
    """解析路径，可选限制在 allowed_root 内"""
    path = Path(os.path.expanduser(target))
    if not path.is_absolute():
        path = cwd / path
    resolved = path.resolve()

    if allowed_root is not None:
        allowed = Path(allowed_root).resolve()
        try:
            resolved.relative_to(allowed)
        except ValueError:
            raise ValueError("path is outside the allowed workspace")

    return resolved
```

在 `preprocess_context_references` 中，默认把 `cwd` 作为 `allowed_root`，用户的引用无法逃出当前工作目录。

## 集成到 Agent 主循环

引用处理应该在用户消息进入 Agent 主循环**之前**完成——对 Agent 来说，它收到的就是一条带有附加上下文的普通消息：

```python
# agent/agent.py 中的消息预处理

from agent.context_references import preprocess_context_references

class Agent:
    def _preprocess_user_message(self, message):
        """预处理用户消息，展开 @ 引用"""
        result = preprocess_context_references(
            message,
            cwd=os.getcwd(),
            context_length=self.llm.context_length,
        )

        if result.blocked:
            # 引用内容过大，提示用户缩小范围
            print(f"⚠️  {result.warnings[-1]}")
            return result.original_message

        if result.warnings:
            for w in result.warnings:
                print(f"⚠️  {w}")

        if result.expanded:
            ref_count = len(result.references)
            print(f"📎 Expanded {ref_count} reference(s), "
                  f"{result.injected_tokens} tokens injected")

        return result.message
```

然后在 REPL 循环中调用：

```python
# agent/__main__.py 的 REPL 循环

while True:
    user_input = input("> ").strip()
    if not user_input:
        continue

    # 预处理引用
    processed = agent._preprocess_user_message(user_input)

    # 正常对话
    response = agent.chat(processed)
    print(response)
```

## 处理效果

用户输入：

```
帮我 review @file:agent/config.py:1-30 和 @diff 的变更
```

处理后 Agent 收到的消息：

````
帮我 review 和 的变更

--- Attached Context ---

📄 @file:agent/config.py:1-30 (150 tokens)
```python
# agent/config.py 的前 30 行内容...
```

🧾 git diff (320 tokens)
```diff
diff --git a/agent/config.py b/agent/config.py
...
```
````

Agent 不需要再调用 `read` 或 `bash` 工具来获取这些信息，直接就能开始分析。

## 与 Hermes Agent 的差异

Hermes 的实现在此基础上多了几层：

1. **异步展开**：URL 抓取等 I/O 操作用 async/await，支持并发展开多个引用
2. **LLM 辅助提取**：URL 内容不是简单去标签，而是用辅助 LLM 提取结构化内容
3. **二进制文件检测**：读取文件前先检查是否为二进制格式（通过 MIME 类型和 `\x00` 字节检测）
4. **rg 加速目录扫描**：目录列表优先用 ripgrep 的 `--files` 模式，比 `os.walk` 快得多

这些优化在生产环境很重要，但核心机制和我们的实现是一致的——正则解析 → 分类展开 → 预算控制 → 拼接结果。

## 小结

上下文引用系统解决的核心问题是**交互效率**：

- 用户不用等 Agent 自己去找信息，直接 `@` 引用即可
- 一条正则覆盖 6 种引用类型，扩展新类型只需加一个展开函数
- 双层预算限制（25% 警告 / 50% 拒绝）防止上下文溢出
- 敏感路径黑名单 + 目录遍历防护保障安全

下一篇我们来解决另一个实际问题——不是所有请求都需要最强的模型，怎么用智能路由节省 API 费用。
