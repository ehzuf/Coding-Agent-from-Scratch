"""
Glob 工具：文件路径模式匹配

对应 reference 中的 tools/GlobTool/

核心功能：
  - 使用 glob 模式匹配文件路径
  - 支持 ** 递归匹配
  - 返回按修改时间排序的结果

常用模式：
  - *.py         匹配当前目录下的 Python 文件
  - **/*.py      递归匹配所有 Python 文件
  - src/**/*.ts  匹配 src 目录下所有 TypeScript 文件
"""

import glob as glob_module
import os
from typing import Any

from .base import Tool


class GlobTool(Tool):
    """文件路径模式匹配工具。"""

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return """使用 glob 模式匹配文件路径。

支持标准 glob 语法：
- * 匹配任意字符（不包括路径分隔符）
- ** 递归匹配任意目录
- ? 匹配单个字符
- [abc] 匹配字符集

参数：
- pattern: glob 模式（必需），如 "**/*.py"
- path: 搜索的根目录（默认当前目录）

返回按修改时间排序的文件列表。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob 模式，如 **/*.py、src/**/*.ts",
                },
                "path": {
                    "type": "string",
                    "description": "搜索的根目录（默认当前目录）",
                },
            },
            "required": ["pattern"],
        }

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True  # 只读操作

    def call(self, input: dict[str, Any]) -> str:
        pattern = input.get("pattern", "")
        path = input.get("path", ".")

        if not pattern:
            return "错误：pattern 参数不能为空"

        # 验证路径
        if not os.path.isdir(path):
            return f"错误：目录不存在: {path}"

        try:
            # 执行 glob 匹配
            # recursive=True 支持 ** 模式
            full_pattern = os.path.join(path, pattern)
            matches = glob_module.glob(full_pattern, recursive=True)

            # 过滤掉目录，只保留文件
            files = [f for f in matches if os.path.isfile(f)]

            if not files:
                return f"未找到匹配的文件: {pattern}"

            # 按修改时间排序（最新的在前）
            files.sort(key=lambda f: os.path.getmtime(f), reverse=True)

            # 构建输出
            lines = [f"找到 {len(files)} 个文件（按修改时间排序）:", ""]
            for i, f in enumerate(files, 1):
                # 获取相对路径
                rel_path = os.path.relpath(f, path) if path != "." else f
                lines.append(f"  {i}. {rel_path}")

            return "\n".join(lines)

        except Exception as e:
            return f"错误：执行 glob 匹配失败 - {e}"
