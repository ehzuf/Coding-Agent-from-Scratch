"""
会话持久化 —— 将消息历史序列化到磁盘，支持 --resume 恢复

对应 reference 中的 history.ts + sessionStorage.ts + sessionRestore.ts

核心设计：
  - 每个会话一个 JSONL 文件，存放在 ~/.coding-agent/sessions/ 目录
  - 每条消息序列化为一行 JSON（JSONL 格式），追加写入
  - 会话元数据（session_id、项目路径、模型、创建时间）保存在文件首行
  - --resume SESSION_ID 从中断处恢复完整消息历史
  - --list-sessions 列出所有可恢复的会话

JSONL 格式示例：
  {"type": "meta", "session_id": "abc123", "project": "/path/to/project", "model": "claude-sonnet-4-20250514", "created_at": "..."}
  {"type": "message", "role": "user", "content": "你好"}
  {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "..."}]}
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _get_sessions_dir() -> Path:
    """获取会话存储目录（~/.coding-agent/sessions/）。"""
    home = Path.home()
    sessions_dir = home / ".coding-agent" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir


def generate_session_id() -> str:
    """生成唯一的会话 ID（短 UUID，前 8 位）。"""
    return uuid.uuid4().hex[:8]


@dataclass
class SessionMeta:
    """会话元数据。"""
    session_id: str
    project: str = ""
    model: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    message_count: int = 0
    first_prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "meta",
            "session_id": self.session_id,
            "project": self.project,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "first_prompt": self.first_prompt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionMeta":
        return cls(
            session_id=data.get("session_id", ""),
            project=data.get("project", ""),
            model=data.get("model", ""),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            message_count=data.get("message_count", 0),
            first_prompt=data.get("first_prompt", ""),
        )


class SessionManager:
    """
    会话持久化管理器。

    负责将消息历史追加写入 JSONL 文件，以及从 JSONL 文件恢复消息历史。

    用法：
        # 创建新会话
        sm = SessionManager(project="/path/to/project", model="claude-sonnet-4-20250514")
        sm.append_message({"role": "user", "content": "你好"})
        sm.append_message({"role": "assistant", "content": [...]})

        # 恢复已有会话
        sm = SessionManager.resume(session_id="abc123")
        messages = sm.load_messages()
    """

    def __init__(
        self,
        session_id: str | None = None,
        project: str = "",
        model: str = "",
    ):
        self.session_id = session_id or generate_session_id()
        self.sessions_dir = _get_sessions_dir()
        self.session_path = self.sessions_dir / f"{self.session_id}.jsonl"
        self._meta = SessionMeta(
            session_id=self.session_id,
            project=project,
            model=model,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self._message_count = 0
        self._file_initialized = False

    def _ensure_file(self) -> None:
        """确保会话文件已创建，并写入元数据首行。"""
        if self._file_initialized:
            return
        if not self.session_path.exists():
            with open(self.session_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._meta.to_dict(), ensure_ascii=False) + "\n")
        self._file_initialized = True

    def _update_meta(self) -> None:
        """更新元数据行（重写文件首行）。"""
        self._meta.updated_at = time.time()
        self._meta.message_count = self._message_count

        if not self.session_path.exists():
            return

        # 读取所有行，替换首行
        lines = self.session_path.read_text(encoding="utf-8").splitlines(keepends=True)
        if lines:
            lines[0] = json.dumps(self._meta.to_dict(), ensure_ascii=False) + "\n"
            self.session_path.write_text("".join(lines), encoding="utf-8")

    def append_message(self, message: dict) -> None:
        """
        追加一条消息到会话文件。

        Args:
            message: 消息字典，如 {"role": "user", "content": "..."}
        """
        self._ensure_file()

        entry = {
            "type": "message",
            "timestamp": time.time(),
            **message,
        }
        with open(self.session_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self._message_count += 1

        # 记录第一条 user 消息作为 first_prompt
        if (
            self._message_count == 1
            and message.get("role") == "user"
            and isinstance(message.get("content"), str)
        ):
            self._meta.first_prompt = message["content"][:100]

        self._update_meta()

    def append_messages(self, messages: list[dict]) -> None:
        """批量追加消息到会话文件。"""
        for msg in messages:
            self.append_message(msg)

    def load_messages(self) -> list[dict]:
        """
        从会话文件加载消息历史。

        Returns:
            消息列表（不含元数据行）
        """
        if not self.session_path.exists():
            return []

        messages = []
        with open(self.session_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") == "meta":
                    # 加载元数据
                    self._meta = SessionMeta.from_dict(entry)
                    continue

                if entry.get("type") == "message":
                    # 还原为标准消息格式（去掉 type 和 timestamp 字段）
                    msg = {k: v for k, v in entry.items() if k not in ("type", "timestamp")}
                    messages.append(msg)

        self._message_count = len(messages)
        return messages

    def get_meta(self) -> SessionMeta:
        """获取会话元数据。"""
        return self._meta

    @classmethod
    def resume(cls, session_id: str) -> "SessionManager":
        """
        从已有会话恢复。

        Args:
            session_id: 会话 ID

        Returns:
            SessionManager 实例

        Raises:
            FileNotFoundError: 会话文件不存在
        """
        sessions_dir = _get_sessions_dir()
        session_path = sessions_dir / f"{session_id}.jsonl"
        if not session_path.exists():
            raise FileNotFoundError(f"会话 '{session_id}' 不存在: {session_path}")

        # 创建实例并加载元数据
        sm = cls(session_id=session_id)
        sm._file_initialized = True

        # 读取元数据
        with open(session_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            if first_line:
                try:
                    meta_data = json.loads(first_line)
                    if meta_data.get("type") == "meta":
                        sm._meta = SessionMeta.from_dict(meta_data)
                except json.JSONDecodeError:
                    pass

        return sm

    @staticmethod
    def list_sessions(
        project: str | None = None,
        limit: int = 20,
    ) -> list[SessionMeta]:
        """
        列出可用会话。

        Args:
            project: 过滤指定项目路径的会话（None 则列出全部）
            limit:   最多返回的会话数

        Returns:
            SessionMeta 列表，按 updated_at 降序排列
        """
        sessions_dir = _get_sessions_dir()
        if not sessions_dir.exists():
            return []

        metas: list[SessionMeta] = []
        for path in sessions_dir.glob("*.jsonl"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if not first_line:
                        continue
                    data = json.loads(first_line)
                    if data.get("type") != "meta":
                        continue
                    meta = SessionMeta.from_dict(data)
                    if project and meta.project != project:
                        continue
                    metas.append(meta)
            except (json.JSONDecodeError, OSError):
                continue

        # 按更新时间降序排列
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas[:limit]

    @staticmethod
    def format_session_list(sessions: list[SessionMeta]) -> str:
        """
        格式化会话列表为可读字符串。

        Returns:
            格式化的会话列表文本
        """
        if not sessions:
            return "没有可用的会话。"

        lines = []
        for meta in sessions:
            # 格式化时间
            created = time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime(meta.created_at),
            )
            # 截断 first_prompt
            prompt_preview = meta.first_prompt[:50]
            if len(meta.first_prompt) > 50:
                prompt_preview += "..."
            if not prompt_preview:
                prompt_preview = "(空)"

            lines.append(
                f"  {meta.session_id}  "
                f"{created}  "
                f"[{meta.message_count} 条消息]  "
                f"{prompt_preview}"
            )

        header = f"可用会话（共 {len(sessions)} 个）:\n"
        return header + "\n".join(lines)
