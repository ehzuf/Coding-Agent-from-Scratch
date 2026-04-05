"""
Auto-Memory —— 跨会话持久记忆

对应 reference 中的 memdir/ + services/extractMemories/

核心设计：
  - 对话结束（LLM 最终回复、无工具调用）时，用 LLM 分析对话提取持久性记忆
  - 记忆存储在 ~/.coding-agent/projects/<path-hash>/memory/ 目录
  - 每条记忆一个 .md 文件，带 frontmatter（name, description, type）
  - MEMORY.md 作为索引文件，列出所有记忆
  - 新会话启动时加载 MEMORY.md 注入 system prompt

记忆类型（参考 Claude Code 四种类型）：
  - user: 用户信息和偏好（角色、习惯、风格）
  - feedback: 用户反馈和纠正（避免的做法、偏好的方式）
  - project: 项目事实（架构、约定、关键路径）
  - reference: 外部引用（文档链接、系统地址）
"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.llm.base import BaseLLM


# ============================================================================
# 路径管理
# ============================================================================

# 默认存储根目录
AGENT_HOME = os.path.expanduser("~/.coding-agent")
PROJECTS_DIR = os.path.join(AGENT_HOME, "projects")
MEMORY_DIRNAME = "memory"
MEMORY_INDEX = "MEMORY.md"

# 记忆文件最大数量
MAX_MEMORY_FILES = 100


def _project_hash(cwd: str) -> str:
    """
    根据项目路径生成唯一 hash（取前 12 位）。

    使用绝对路径的 SHA256，确保同一项目始终映射到同一目录。
    """
    abs_path = os.path.abspath(cwd)
    return hashlib.sha256(abs_path.encode()).hexdigest()[:12]


def get_memory_dir(cwd: str) -> str:
    """获取项目的记忆目录路径。"""
    return os.path.join(PROJECTS_DIR, _project_hash(cwd), MEMORY_DIRNAME)


def ensure_memory_dir(cwd: str) -> str:
    """确保记忆目录存在，返回目录路径。"""
    memory_dir = get_memory_dir(cwd)
    os.makedirs(memory_dir, exist_ok=True)
    return memory_dir


# ============================================================================
# 记忆文件操作
# ============================================================================

@dataclass
class MemoryEntry:
    """一条记忆的元数据。"""
    filename: str
    filepath: str
    name: str = ""
    description: str = ""
    memory_type: str = ""  # user, feedback, project, reference
    content: str = ""


def _parse_frontmatter(content: str) -> dict:
    """
    解析 markdown frontmatter（--- 包围的 YAML-like 内容）。

    简化实现：只支持 key: value 格式，不引入 yaml 依赖。
    """
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


def scan_memory_files(memory_dir: str) -> list[MemoryEntry]:
    """
    扫描记忆目录，读取所有 .md 文件（排除 MEMORY.md）的 frontmatter。

    Returns:
        按修改时间降序排列的 MemoryEntry 列表
    """
    entries = []
    if not os.path.isdir(memory_dir):
        return entries

    for filename in os.listdir(memory_dir):
        if not filename.endswith(".md") or filename == MEMORY_INDEX:
            continue

        filepath = os.path.join(memory_dir, filename)
        if not os.path.isfile(filepath):
            continue

        try:
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
        except Exception:
            continue

    # 按修改时间降序排列
    entries.sort(key=lambda e: os.path.getmtime(e.filepath), reverse=True)
    return entries[:MAX_MEMORY_FILES]


def load_memory_index(cwd: str) -> str | None:
    """
    加载项目的 MEMORY.md 索引内容。

    Returns:
        索引内容字符串，不存在则返回 None
    """
    memory_dir = get_memory_dir(cwd)
    index_path = os.path.join(memory_dir, MEMORY_INDEX)
    if not os.path.isfile(index_path):
        return None
    try:
        content = Path(index_path).read_text(encoding="utf-8").strip()
        return content if content else None
    except Exception:
        return None


def format_memory_manifest(entries: list[MemoryEntry]) -> str:
    """
    将记忆列表格式化为清单文本（提取 prompt 中使用）。
    """
    lines = []
    for e in entries:
        tag = f"[{e.memory_type}] " if e.memory_type else ""
        desc = f": {e.description}" if e.description else ""
        lines.append(f"- {tag}{e.filename}{desc}")
    return "\n".join(lines)


# ============================================================================
# 记忆提取 Prompt
# ============================================================================

EXTRACT_PROMPT = """你是记忆提取子代理。分析上面的对话历史，提取需要跨会话记住的持久性信息。

## 记忆类型

1. **user**：用户信息和偏好
   - 用户角色、技术背景
   - 编码风格偏好（命名、注释、格式）
   - 沟通偏好（语言、详细程度）

2. **feedback**：用户反馈和纠正
   - 用户指出的错误做法
   - 明确要求的行为调整
   - 应该避免的模式

3. **project**：项目事实
   - 技术栈、架构决策
   - 关键文件路径和用途
   - 项目约定和规范

4. **reference**：外部引用
   - 文档链接
   - 系统地址
   - 相关资源

## 不应保存的内容

- 可从代码直接推断的信息（函数签名、import 路径等）
- 临时性的、只在当前会话有用的信息
- 敏感信息（API key、密码、token）

## 输出格式

如果有需要保存的记忆，输出以下 JSON 格式：

```json
[
  {{
    "filename": "记忆文件名.md",
    "name": "记忆名称",
    "description": "一句话描述",
    "type": "user|feedback|project|reference",
    "content": "记忆的详细内容（markdown 格式）"
  }}
]
```

如果没有需要保存的新记忆，输出空数组 `[]`。

## 已有记忆

{existing_memories}

检查已有记忆，避免重复。如果新信息属于已有记忆的范畴，在 filename 中使用相同文件名（将更新该文件）。

## 注意

- 只提取真正持久、跨会话有用的信息
- 每条记忆聚焦一个主题
- 内容简洁精炼，不要冗长
- 文件名使用英文下划线格式（如 user_preferences.md）
"""


# ============================================================================
# AutoMemory 类
# ============================================================================

@dataclass
class AutoMemory:
    """
    跨会话持久记忆管理器。

    在对话结束时（LLM 最终回复，无工具调用）分析对话，
    提取持久性记忆并保存到磁盘。

    用法：
        am = AutoMemory(llm=llm, cwd="/path/to/project")

        # 对话结束后提取记忆
        am.extract_and_save(messages)

        # 加载已有记忆（注入 system prompt）
        memory_prompt = am.load_memory_prompt()
    """

    llm: BaseLLM
    cwd: str
    enabled: bool = True
    _extract_count: int = 0

    def load_memory_prompt(self) -> str | None:
        """
        加载记忆提示，用于注入 system prompt。

        Returns:
            记忆提示字符串，没有记忆则返回 None
        """
        if not self.enabled:
            return None

        index_content = load_memory_index(self.cwd)
        if not index_content:
            return None

        memory_dir = get_memory_dir(self.cwd)

        return (
            "## 跨会话记忆\n\n"
            f"以下是该项目的持久记忆（存储在 `{memory_dir}`）：\n\n"
            + index_content
        )

    def extract_and_save(self, messages: list[dict]) -> int:
        """
        从对话历史中提取记忆并保存。

        Args:
            messages: 当前对话消息历史

        Returns:
            新保存的记忆数量
        """
        if not self.enabled:
            return 0

        # 对话太短不提取
        if len(messages) < 4:
            return 0

        try:
            # 扫描已有记忆
            memory_dir = ensure_memory_dir(self.cwd)
            existing = scan_memory_files(memory_dir)
            existing_manifest = format_memory_manifest(existing) if existing else "（暂无已有记忆）"

            # 构建对话摘要
            summary = self._build_conversation_summary(messages)

            # 构建提取 prompt
            prompt = EXTRACT_PROMPT.format(existing_memories=existing_manifest)

            # 调用 LLM 提取
            extract_messages = [
                {"role": "user", "content": summary},
                {"role": "assistant", "content": "我已阅读对话历史，准备提取持久性记忆。"},
                {"role": "user", "content": prompt},
            ]

            response = self.llm.chat(extract_messages)
            text = response.text.strip()

            # 解析 JSON 结果
            memories = self._parse_memories(text)
            if not memories:
                return 0

            # 保存记忆文件
            saved = 0
            for mem in memories:
                if self._save_memory(memory_dir, mem):
                    saved += 1

            # 更新索引
            if saved > 0:
                self._update_index(memory_dir)
                self._extract_count += 1

            return saved

        except Exception:
            return 0

    def _build_conversation_summary(self, messages: list[dict]) -> str:
        """构建对话摘要，用于记忆提取。"""
        parts = []
        total_chars = 0
        max_chars = 20000

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
                            texts.append(f"[工具: {block.get('name', '')}]")
                        elif block.get("type") == "tool_result":
                            result = block.get("content", "")
                            if isinstance(result, str) and len(result) > 100:
                                result = result[:100] + "..."
                            texts.append(f"[结果: {result}]")
                text = "\n".join(texts)
            else:
                text = str(content)

            if len(text) > 1000:
                text = text[:1000] + "..."

            line = f"[{role}] {text}"
            if total_chars + len(line) > max_chars:
                break
            parts.append(line)
            total_chars += len(line)

        return "\n\n".join(parts)

    def _parse_memories(self, text: str) -> list[dict]:
        """
        从 LLM 响应中解析记忆 JSON。

        支持 markdown 代码块包裹和裸 JSON。
        """
        import json

        # 尝试从 markdown 代码块中提取
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        json_str = match.group(1) if match else text

        try:
            result = json.loads(json_str)
            if isinstance(result, list):
                return [m for m in result if isinstance(m, dict) and m.get("filename")]
            return []
        except (json.JSONDecodeError, ValueError):
            return []

    def _save_memory(self, memory_dir: str, mem: dict) -> bool:
        """
        保存单条记忆到文件。

        Args:
            memory_dir: 记忆目录
            mem: {"filename", "name", "description", "type", "content"}

        Returns:
            是否保存成功
        """
        filename = mem.get("filename", "")
        if not filename or not filename.endswith(".md"):
            filename = filename + ".md" if filename else "unnamed.md"

        # 安全检查：文件名不能包含路径分隔符
        if "/" in filename or "\\" in filename:
            return False

        filepath = os.path.join(memory_dir, filename)

        # 构建文件内容（带 frontmatter）
        name = mem.get("name", filename.replace(".md", ""))
        description = mem.get("description", "")
        mem_type = mem.get("type", "project")
        content = mem.get("content", "")

        file_content = f"""---
name: {name}
description: {description}
type: {mem_type}
---

{content}
"""

        try:
            Path(filepath).write_text(file_content, encoding="utf-8")
            return True
        except Exception:
            return False

    def _update_index(self, memory_dir: str) -> None:
        """
        重建 MEMORY.md 索引文件。

        扫描所有记忆文件，按类型分组生成索引。
        """
        entries = scan_memory_files(memory_dir)
        if not entries:
            return

        # 按类型分组
        by_type: dict[str, list[MemoryEntry]] = {}
        for e in entries:
            t = e.memory_type or "other"
            by_type.setdefault(t, []).append(e)

        # 类型显示顺序
        type_order = ["user", "feedback", "project", "reference", "other"]
        type_labels = {
            "user": "用户偏好",
            "feedback": "反馈与纠正",
            "project": "项目事实",
            "reference": "参考引用",
            "other": "其他",
        }

        lines = ["# 项目记忆索引", ""]
        for t in type_order:
            group = by_type.get(t)
            if not group:
                continue
            lines.append(f"## {type_labels.get(t, t)}")
            lines.append("")
            for e in group:
                desc = f" — {e.description}" if e.description else ""
                lines.append(f"- [{e.name}]({e.filename}){desc}")
            lines.append("")

        index_path = os.path.join(memory_dir, MEMORY_INDEX)
        try:
            Path(index_path).write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    def list_memories(self) -> list[MemoryEntry]:
        """列出所有记忆。"""
        memory_dir = get_memory_dir(self.cwd)
        return scan_memory_files(memory_dir)

    def get_memory_dir(self) -> str:
        """获取记忆目录路径。"""
        return get_memory_dir(self.cwd)
