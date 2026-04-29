"""
上下文管理 —— 分层加载项目记忆文件并组装系统提示

对应 reference 中的 utils/claudemd.ts + context.ts

记忆文件加载顺序（优先级从低到高）：
  1. 用户级：~/.coding-agent/AGENTS.md —— 个人全局偏好
  2. 用户规则：~/.coding-agent/rules/*.md —— 个人全局规则
  3. 项目级（从根目录到 cwd，越近优先级越高）：
     - AGENTS.md
     - .coding-agent/AGENTS.md
     - .coding-agent/rules/*.md
  4. 本地级：AGENTS.local.md —— 个人项目配置（应 gitignore）

@include 指令：
  - 在记忆文件中使用 @path 引用其他文件
  - 支持相对路径 @./file.md、绝对路径 @/path/to/file.md、home 路径 @~/file.md
  - 代码块内的 @path 不会被解析
  - 防止循环引用
  - 不存在的文件静默忽略
"""

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


# 记忆文件类型
MEMORY_TYPES = ("User", "Project", "Local")

# @include 最大递归深度
MAX_INCLUDE_DEPTH = 5

# 记忆文件最大字符数（超出截断）
MAX_MEMORY_CHARACTERS = 40000

# @include 指令正则：匹配行首或空格后的 @path（排除代码块内）
_INCLUDE_PATTERN = re.compile(r"(?:^|\s)@((?:\./|~/|/)[^\s]+)", re.MULTILINE)

# 允许 @include 的文本文件扩展名
_TEXT_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".xml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".sh", ".bash",
    ".sql", ".html", ".css", ".vue", ".svelte",
    ".env", ".ini", ".cfg", ".conf", ".config",
}


def _get_user_memory_dir() -> Path:
    """获取用户级记忆目录：~/.coding-agent/"""
    return Path.home() / ".coding-agent"


@dataclass
class MemoryFile:
    """一个已加载的记忆文件。"""
    path: str           # 文件绝对路径
    content: str        # 文件内容（含 @include 展开后的内容）
    memory_type: str    # User / Project / Local
    source: str         # 来源描述（如 "~/.coding-agent/AGENTS.md"）


def _resolve_include_path(raw_path: str, base_dir: str) -> str | None:
    """
    解析 @include 路径为绝对路径。

    支持：
      - @./relative/path  → 相对于当前文件目录
      - @~/home/path      → 相对于用户 home
      - @/absolute/path   → 绝对路径

    Returns:
        绝对路径字符串，如果路径无效则返回 None
    """
    if raw_path.startswith("~/"):
        resolved = Path.home() / raw_path[2:]
    elif raw_path.startswith("./"):
        resolved = Path(base_dir) / raw_path[2:]
    elif raw_path.startswith("/"):
        resolved = Path(raw_path)
    else:
        return None

    resolved = resolved.resolve()

    # 检查扩展名
    if resolved.suffix.lower() not in _TEXT_EXTENSIONS:
        return None

    return str(resolved)


def _strip_code_blocks(content: str) -> str:
    """
    从内容中移除代码块（``` ... ```），返回非代码部分。
    用于在非代码区域中查找 @include 指令。
    """
    return re.sub(r"```[\s\S]*?```", "", content)


def _extract_include_paths(content: str, base_dir: str) -> list[str]:
    """
    从文件内容中提取 @include 路径列表。

    只在非代码块区域查找。

    Returns:
        解析后的绝对路径列表
    """
    text_only = _strip_code_blocks(content)
    paths = []
    for match in _INCLUDE_PATTERN.finditer(text_only):
        raw = match.group(1)
        resolved = _resolve_include_path(raw, base_dir)
        if resolved:
            paths.append(resolved)
    return paths


def _read_file_safe(path: str) -> str | None:
    """安全读取文件，失败返回 None。"""
    try:
        content = Path(path).read_text(encoding="utf-8")
        return content if content.strip() else None
    except (OSError, UnicodeDecodeError):
        return None


def _process_memory_file(
    file_path: str,
    memory_type: str,
    processed: set[str],
    depth: int = 0,
) -> list[MemoryFile]:
    """
    处理单个记忆文件，递归展开 @include。

    Args:
        file_path:    文件绝对路径
        memory_type:  User / Project / Local
        processed:    已处理的路径集合（防循环）
        depth:        当前递归深度

    Returns:
        MemoryFile 列表（主文件 + include 的文件）
    """
    normalized = os.path.normpath(file_path)
    if normalized in processed or depth >= MAX_INCLUDE_DEPTH:
        return []

    processed.add(normalized)

    content = _read_file_safe(file_path)
    if not content:
        return []

    result = []

    # 主文件排在前面（父优先于子，与 Claude Code 一致）
    # 生成友好的 source 描述
    home = str(Path.home())
    display_path = file_path.replace(home, "~") if file_path.startswith(home) else file_path

    result.append(MemoryFile(
        path=file_path,
        content=content,
        memory_type=memory_type,
        source=display_path,
    ))

    # 提取并递归处理 @include（被引用的文件排在后面）
    base_dir = str(Path(file_path).parent)
    include_paths = _extract_include_paths(content, base_dir)

    for inc_path in include_paths:
        included = _process_memory_file(inc_path, memory_type, processed, depth + 1)
        result.extend(included)

    return result


def _load_rules_dir(
    rules_dir: str,
    memory_type: str,
    processed: set[str],
) -> list[MemoryFile]:
    """
    加载 rules 目录下所有 .md 文件。

    文件按名称排序，保证确定性顺序。
    """
    rules_path = Path(rules_dir)
    if not rules_path.is_dir():
        return []

    result = []
    try:
        md_files = sorted(rules_path.glob("*.md"))
    except OSError:
        return []

    for md_file in md_files:
        files = _process_memory_file(str(md_file), memory_type, processed)
        result.extend(files)

    return result


def load_memory_files(cwd: str = ".") -> list[MemoryFile]:
    """
    发现并加载所有记忆文件。

    加载顺序（优先级从低到高，后加载的优先级更高）：
      1. 用户级 ~/.coding-agent/AGENTS.md
      2. 用户规则 ~/.coding-agent/rules/*.md
      3. 项目级（从根目录到 cwd，越近优先级越高）：
         - AGENTS.md
         - .coding-agent/AGENTS.md
         - .coding-agent/rules/*.md
      4. 本地级 AGENTS.local.md（仅 cwd 向上查找到的最近一个）

    Returns:
        MemoryFile 列表，按加载顺序排列
    """
    result: list[MemoryFile] = []
    processed: set[str] = set()

    # --- 1. 用户级 ---
    user_dir = _get_user_memory_dir()

    # ~/.coding-agent/AGENTS.md
    user_agents = user_dir / "AGENTS.md"
    result.extend(_process_memory_file(str(user_agents), "User", processed))

    # ~/.coding-agent/rules/*.md
    user_rules = user_dir / "rules"
    result.extend(_load_rules_dir(str(user_rules), "User", processed))

    # --- 2. 项目级（从根向 cwd 遍历，越近优先级越高）---
    abs_cwd = Path(cwd).resolve()
    dirs: list[Path] = []
    current = abs_cwd
    while True:
        dirs.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    # 从根目录向 cwd 方向遍历
    for d in reversed(dirs):
        # AGENTS.md
        agents_md = d / "AGENTS.md"
        result.extend(_process_memory_file(str(agents_md), "Project", processed))

        # .coding-agent/AGENTS.md
        dot_agents = d / ".coding-agent" / "AGENTS.md"
        result.extend(_process_memory_file(str(dot_agents), "Project", processed))

        # .coding-agent/rules/*.md
        dot_rules = d / ".coding-agent" / "rules"
        result.extend(_load_rules_dir(str(dot_rules), "Project", processed))

    # --- 3. 本地级（从 cwd 向上查找最近的 AGENTS.local.md）---
    current = abs_cwd
    while True:
        local_md = current / "AGENTS.local.md"
        if local_md.exists():
            result.extend(_process_memory_file(str(local_md), "Local", processed))
            break  # 只加载最近的一个
        parent = current.parent
        if parent == current:
            break
        current = parent

    return result


def build_system_prompt(
    base_system: str | None = None,
    cwd: str = ".",
    include_date: bool = True,
) -> str | None:
    """
    构建完整的系统提示。

    包含：
      1. 基础系统提示（如果提供）
      2. 记忆文件内容（分层加载）
      3. 上下文信息（日期、工作目录）

    Args:
        base_system: 基础系统提示
        cwd: 工作目录
        include_date: 是否包含日期信息

    Returns:
        完整的系统提示字符串，如果没有任何内容则返回 None
    """
    parts = []

    # 1. 基础系统提示
    if base_system:
        parts.append(base_system)

    # 2. 记忆文件
    memory_files = load_memory_files(cwd)
    if memory_files:
        # 计算总大小
        total_size = sum(len(mf.content) for mf in memory_files)

        # 截断保护：优先保留高优先级文件（列表后面的）
        if total_size > MAX_MEMORY_CHARACTERS:
            selected_files: list[MemoryFile] = []
            remaining = MAX_MEMORY_CHARACTERS

            # 从后往前遍历（高优先级优先）
            for mf in reversed(memory_files):
                mf_size = len(mf.content)
                if mf_size <= remaining:
                    selected_files.append(mf)
                    remaining -= mf_size

            # 恢复原始顺序（低优先级在前，高优先级在后）
            selected_files.reverse()

            # 警告被跳过的文件
            skipped = [mf for mf in memory_files if mf not in selected_files]
            if skipped:
                print("[警告] 以下记忆文件因超出上限被跳过：", file=sys.stderr)
                for mf in skipped:
                    print(f"  - {mf.source} ({len(mf.content)} 字符)", file=sys.stderr)
        else:
            selected_files = memory_files

        memory_parts = []
        for mf in selected_files:
            label = f"[{mf.memory_type}] {mf.source}"
            memory_parts.append(f"### {label}\n\n{mf.content}")

        if memory_parts:
            parts.append(
                "## 项目规范\n\n"
                "以下是项目的编码规范和指令，请严格遵守。\n\n"
                + "\n\n".join(memory_parts)
            )

    # 3. 上下文信息
    context_parts = []

    if include_date:
        now = datetime.now()
        context_parts.append(f"当前日期时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")

    abs_cwd = os.path.abspath(cwd)
    context_parts.append(f"工作目录: {abs_cwd}")

    if context_parts:
        parts.append("## 上下文信息\n\n" + "\n".join(context_parts))

    if not parts:
        return None

    return "\n\n".join(parts)


def get_context_info(cwd: str = ".") -> dict:
    """
    获取上下文信息字典。

    Returns:
        {
            "cwd": 工作目录绝对路径,
            "date": 当前日期时间字符串,
            "memory_files": 已加载的记忆文件列表,
            "agents_md_loaded": 是否加载了任何记忆文件（兼容旧接口）,
            "agents_md_path": 第一个项目级记忆文件路径（兼容旧接口）,
        }
    """
    now = datetime.now()
    memory_files = load_memory_files(cwd)

    # 兼容旧接口：找第一个 Project 类型的文件
    project_files = [mf for mf in memory_files if mf.memory_type == "Project"]
    first_project = project_files[0].path if project_files else None

    return {
        "cwd": os.path.abspath(cwd),
        "date": now.strftime("%Y-%m-%d %H:%M:%S"),
        "memory_files": memory_files,
        "agents_md_loaded": len(memory_files) > 0,
        "agents_md_path": first_project,
    }
