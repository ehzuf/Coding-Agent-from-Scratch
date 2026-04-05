"""
文件读取工具

对应 reference 中的 tools/FileReadTool/

核心功能：
  - 读取文件内容
  - 支持指定偏移量和行数限制（用于读取大文件的部分内容）
  - 自动处理多种文本编码

注意：这是给 LLM 看文件内容的工具，不是让 LLM 直接访问文件系统。
"""

import os
from typing import Any

from .base import Tool


class ReadTool(Tool):
    """读取文件内容的工具。"""

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return """读取文件内容。

可以读取文本文件的内容，支持指定偏移量和行数限制。
返回的内容会带行号，格式为 "  1  第一行内容"。

参数：
- file_path: 文件路径（必需）
- offset: 从第几行开始读取（默认从第 1 行开始）
- limit: 最多读取多少行（默认读取全部）

注意：不要用于读取二进制文件。"""

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
                    "description": "从第几行开始读取（默认 1，即第一行，1-based）",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取多少行（默认读取全部）",
                },
            },
            "required": ["file_path"],
        }

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True  # 只读操作

    def call(self, input: dict[str, Any]) -> str:
        file_path = input.get("file_path", "")
        offset = input.get("offset", 1)
        limit = input.get("limit")

        if not file_path:
            return "错误：file_path 参数不能为空"

        # 检查文件是否存在
        if not os.path.exists(file_path):
            return f"错误：文件不存在: {file_path}"

        # 检查是否是文件
        if not os.path.isfile(file_path):
            return f"错误：不是文件: {file_path}"

        try:
            # 尝试多种编码
            encodings = ["utf-8", "gbk", "latin-1"]
            content = None
            used_encoding = None

            for encoding in encodings:
                try:
                    with open(file_path, "r", encoding=encoding) as f:
                        content = f.readlines()
                    used_encoding = encoding
                    break
                except UnicodeDecodeError:
                    continue

            if content is None:
                return f"错误：无法解码文件（尝试了编码: {encodings}），可能是二进制文件"

            # 应用 offset 和 limit（offset 是 1-based）
            if offset > 1:
                content = content[offset - 1:]

            if limit is not None and limit > 0:
                content = content[:limit]

            # 添加行号
            total_lines = len(content)
            start_line = offset

            lines_with_numbers = []
            for i, line in enumerate(content):
                line_num = start_line + i
                # 去掉末尾换行符，统一处理
                line_content = line.rstrip("\n\r")
                lines_with_numbers.append(f"{line_num:6}\t{line_content}")

            result = "\n".join(lines_with_numbers)

            # 添加摘要信息
            summary = f"文件: {file_path}\n"
            summary += f"编码: {used_encoding}\n"
            summary += f"显示: 第 {start_line}-{start_line + total_lines - 1} 行\n"
            summary += "-" * 40 + "\n"

            return summary + result

        except Exception as e:
            return f"错误：读取文件失败 - {e}"
