"""
Grep 工具：正则内容搜索

对应 reference 中的 tools/GrepTool/

核心功能：
  - 在文件中搜索正则表达式
  - 支持多文件搜索
  - 返回匹配的行和上下文

设计思想：
  - 帮助 LLM 快速定位代码中的特定内容
  - 支持正则表达式，比简单字符串匹配更强大
"""

import os
import re
from typing import Any

from .base import Tool


class GrepTool(Tool):
    """正则内容搜索工具。"""

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return """在文件中搜索正则表达式。

使用正则表达式搜索文件内容，返回匹配的行及其行号。
支持搜索单个文件或整个目录。

参数：
- pattern: 正则表达式（必需）
- path: 要搜索的文件或目录（默认当前目录）
- include: 文件类型过滤，如 "*.py"（仅在搜索目录时有效）

返回格式：
  file_path:line_number: 匹配内容

注意：pattern 是完整的 Python 正则表达式。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "正则表达式模式",
                },
                "path": {
                    "type": "string",
                    "description": "要搜索的文件或目录（默认当前目录）",
                },
                "include": {
                    "type": "string",
                    "description": "文件类型过滤，如 *.py（仅在搜索目录时有效）",
                },
            },
            "required": ["pattern"],
        }

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True  # 只读操作

    def call(self, input: dict[str, Any]) -> str:
        pattern = input.get("pattern", "")
        path = input.get("path", ".")
        include = input.get("include")

        if not pattern:
            return "错误：pattern 参数不能为空"

        # 编译正则表达式
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"错误：无效的正则表达式 - {e}"

        # 确定搜索的文件列表
        files_to_search = []

        if os.path.isfile(path):
            files_to_search.append(path)
        elif os.path.isdir(path):
            # 遍历目录
            for root, dirs, files in os.walk(path):
                for filename in files:
                    # 应用文件过滤
                    if include:
                        # 简单支持 *.py 格式
                        if include.startswith("*."):
                            ext = include[1:]  # .py
                            if not filename.endswith(ext):
                                continue
                        elif not filename.endswith(include):
                            continue

                    full_path = os.path.join(root, filename)
                    files_to_search.append(full_path)
        else:
            return f"错误：路径不存在: {path}"

        # 执行搜索
        matches = []

        for file_path in files_to_search:
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_num, line in enumerate(f, 1):
                        if regex.search(line):
                            # 去掉换行符
                            line_content = line.rstrip("\n\r")
                            matches.append({
                                "file": file_path,
                                "line": line_num,
                                "content": line_content,
                            })
            except Exception:
                # 跳过无法读取的文件（如二进制文件）
                continue

        if not matches:
            return f"未找到匹配: {pattern}"

        # 构建输出
        lines = [f"找到 {len(matches)} 处匹配:", ""]

        for match in matches:
            rel_path = os.path.relpath(match["file"])
            lines.append(f"{rel_path}:{match['line']}: {match['content']}")

        return "\n".join(lines)
