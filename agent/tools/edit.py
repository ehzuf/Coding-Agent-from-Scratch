"""
文件编辑工具

对应 reference 中的 tools/FileEditTool/

核心功能：
  - 精确字符串替换
  - 支持单次替换或全部替换
  - 替换前会验证 old_string 是否存在

设计思想：
  - 使用精确匹配而非行号，避免行号变化导致错误
  - 要求 old_string 唯一（除非 replace_all=True）
  - 返回详细的编辑结果
"""

import os
from typing import Any

from .base import Tool


class EditTool(Tool):
    """编辑文件的工具（精确字符串替换）。"""

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return """通过精确字符串替换来编辑文件。

在文件中查找 old_string，并将其替换为 new_string。
这是比重写整个文件更精确的编辑方式。

参数：
- file_path: 文件路径（必需）
- old_string: 要查找的字符串（必需）
- new_string: 替换后的字符串（必需）
- replace_all: 是否替换所有匹配项（默认只替换第一个）

注意：
1. old_string 必须完全匹配（包括空格和换行）
2. 默认要求 old_string 只出现一次，避免意外替换
3. 如果要替换多个相同内容，设置 replace_all=true"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要编辑的文件路径",
                },
                "old_string": {
                    "type": "string",
                    "description": "要查找的字符串（必须完全匹配）",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的字符串",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配项（默认 false，只替换第一个）",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    def call(self, input: dict[str, Any]) -> str:
        file_path = input.get("file_path", "")
        old_string = input.get("old_string", "")
        new_string = input.get("new_string", "")
        replace_all = input.get("replace_all", False)

        # 参数验证
        if not file_path:
            return "错误：file_path 参数不能为空"
        if not old_string:
            return "错误：old_string 参数不能为空"

        # 检查文件是否存在
        if not os.path.exists(file_path):
            return f"错误：文件不存在: {file_path}"

        if not os.path.isfile(file_path):
            return f"错误：不是文件: {file_path}"

        try:
            # 读取文件内容
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 检查 old_string 是否存在
            if old_string not in content:
                return f"错误：未找到要替换的字符串。请确保 old_string 与文件中的内容完全一致（包括空格和换行）。"

            # 统计出现次数
            count = content.count(old_string)

            if count > 1 and not replace_all:
                return f"错误：找到 {count} 处匹配。如果确实要替换所有匹配项，请设置 replace_all=true。否则请提供更精确的 old_string 以确保唯一匹配。"

            # 执行替换
            if replace_all:
                new_content = content.replace(old_string, new_string)
                replaced_count = count
            else:
                new_content = content.replace(old_string, new_string, 1)
                replaced_count = 1

            # 写回文件
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            return f"成功编辑文件: {file_path}\n替换了 {replaced_count} 处"

        except UnicodeDecodeError:
            return "错误：无法解码文件，可能是二进制文件"
        except Exception as e:
            return f"错误：编辑文件失败 - {e}"
