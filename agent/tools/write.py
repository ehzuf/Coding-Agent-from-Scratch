"""
文件写入工具

对应 reference 中的 tools/FileWriteTool/

核心功能：
  - 写入或覆盖文件
  - 自动创建父目录
  - 支持 UTF-8 编码

安全考虑：
  - 会覆盖现有文件，需要谨慎使用
  - 后续会添加权限系统
"""

import os
from typing import Any

from .base import Tool


class WriteTool(Tool):
    """写入文件的工具。"""

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return """写入或覆盖文件。

将内容写入指定文件。如果文件不存在会创建，如果存在会覆盖。
会自动创建所需的父目录。

参数：
- file_path: 文件路径（必需）
- content: 要写入的内容（必需）

注意：此操作会覆盖现有文件，请谨慎使用。"""

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

        if not file_path:
            return "错误：file_path 参数不能为空"

        try:
            # 确保父目录存在
            parent_dir = os.path.dirname(file_path)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)

            # 写入文件
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            # 统计信息
            lines = content.count("\n") + (1 if content else 0)
            chars = len(content)

            return f"成功写入文件: {file_path}\n行数: {lines}, 字符数: {chars}"

        except Exception as e:
            return f"错误：写入文件失败 - {e}"
