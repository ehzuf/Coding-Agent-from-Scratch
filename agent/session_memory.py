"""
Session Memory —— 会话记忆（定期生成结构化笔记）

对应 reference 中的 services/SessionMemory/sessionMemory.ts + prompts.ts

核心设计：
  - 定期用 LLM 从对话历史中提取结构化笔记
  - 笔记存储在内存中，格式为 markdown
  - 上下文压缩时将笔记注入，防止关键信息丢失
  - 与 session persistence 配合：笔记随会话持久化

触发条件：
  - 每隔 N 次工具调用后触发一次更新
  - 消息历史超过一定 token 数时触发
  - 手动触发

笔记模板（参考 Claude Code）：
  - Session Title: 会话标题
  - Current State: 当前进展
  - Task Specification: 用户需求
  - Key Files: 关键文件
  - Errors & Corrections: 错误和修正
  - Worklog: 工作日志
"""

from dataclasses import dataclass, field
from agent.llm.base import BaseLLM


# 默认：每 3 次工具调用后更新一次（与 Claude Code 的 toolCallsBetweenUpdates 一致）
DEFAULT_UPDATE_INTERVAL = 3

# 笔记最大 token 数（估算）
MAX_NOTES_TOKENS = 4000

# 笔记模板
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

# 提取笔记的 prompt
EXTRACT_PROMPT_TEMPLATE = """根据以下对话历史，更新会话笔记。

当前笔记内容：
<current_notes>
{current_notes}
</current_notes>

请基于对话内容更新笔记。规则：
1. 保留所有 # 标题行和 _斜体描述_ 行，只更新描述行下方的实际内容
2. 写具体、信息密集的内容：包含文件路径、函数名、错误信息、命令等细节
3. Current State 始终反映最新进展
4. 如果某个章节没有新信息，保持原样即可
5. 每个章节控制在 500 字以内
6. 不要提及"笔记提取"或这条指令本身

直接输出更新后的完整笔记内容（markdown 格式）："""


@dataclass
class SessionMemory:
    """
    会话记忆管理器。

    定期从对话历史中提取结构化笔记，用于：
      1. 上下文压缩时保留关键信息
      2. 会话恢复时提供上下文摘要

    用法：
        sm = SessionMemory(llm=llm)

        # 每次工具调用后检查是否需要更新
        sm.maybe_update(messages)

        # 获取当前笔记（注入到压缩后的上下文中）
        notes = sm.get_notes()
    """

    llm: BaseLLM
    notes: str = ""
    update_interval: int = DEFAULT_UPDATE_INTERVAL
    _tool_calls_since_update: int = 0
    _initialized: bool = False
    _update_count: int = 0

    def record_tool_call(self) -> None:
        """记录一次工具调用。"""
        self._tool_calls_since_update += 1

    def should_update(self) -> bool:
        """判断是否需要更新笔记。"""
        return self._tool_calls_since_update >= self.update_interval

    def maybe_update(self, messages: list[dict]) -> bool:
        """
        检查并在需要时更新笔记。

        Args:
            messages: 当前对话消息历史

        Returns:
            是否执行了更新
        """
        if not self.should_update():
            return False

        self.update(messages)
        return True

    def update(self, messages: list[dict]) -> None:
        """
        从对话历史中提取/更新笔记。

        使用 LLM 分析消息历史，生成结构化笔记。

        Args:
            messages: 当前对话消息历史
        """
        current = self.notes if self.notes else SESSION_MEMORY_TEMPLATE

        # 构建精简的对话摘要（只取文本内容，避免传递过多工具细节）
        conversation_summary = self._build_conversation_summary(messages)

        prompt = EXTRACT_PROMPT_TEMPLATE.format(current_notes=current)

        try:
            # 用 LLM 提取笔记（使用对话上下文 + 提取指令）
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
            # 提取失败不影响主流程
            pass

        self._tool_calls_since_update = 0
        self._initialized = True

    def _build_conversation_summary(self, messages: list[dict]) -> str:
        """
        构建对话摘要，用于笔记提取。

        只提取文本内容，控制长度。
        """
        parts = []
        total_chars = 0
        max_chars = 30000  # 控制输入大小

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # content blocks：提取文本
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

            # 截断单条消息
            if len(text) > 2000:
                text = text[:2000] + "..."

            line = f"[{role}] {text}"
            if total_chars + len(line) > max_chars:
                break
            parts.append(line)
            total_chars += len(line)

        return "\n\n".join(parts)

    def get_notes(self) -> str:
        """获取当前笔记内容。"""
        return self.notes

    def get_notes_for_injection(self) -> str | None:
        """
        获取用于注入 system prompt 的笔记。

        如果笔记为空或未初始化，返回 None。
        """
        if not self.notes or not self._initialized:
            return None

        return (
            "## 会话笔记\n\n"
            "以下是本次会话的自动提取笔记，包含关键信息和进展状态：\n\n"
            + self.notes
        )

    def to_dict(self) -> dict:
        """序列化为字典（用于 session 持久化）。"""
        return {
            "notes": self.notes,
            "update_count": self._update_count,
            "initialized": self._initialized,
        }

    @classmethod
    def from_dict(cls, data: dict, llm: BaseLLM) -> "SessionMemory":
        """从字典恢复（用于 session 持久化）。"""
        sm = cls(llm=llm)
        sm.notes = data.get("notes", "")
        sm._update_count = data.get("update_count", 0)
        sm._initialized = data.get("initialized", False)
        return sm
