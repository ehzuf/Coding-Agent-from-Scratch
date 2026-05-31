"""
语义级代码搜索工具

基于 tree-sitter 解析源代码的语法树，提取函数、类、方法等符号信息。
相比 GrepTool 的纯文本匹配，SymbolSearchTool 能区分定义和引用，
理解代码的层次结构（类→方法的包含关系）。

依赖：pip install tree-sitter tree-sitter-python
"""

import os
from dataclasses import dataclass
from typing import Any

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from .base import Tool

PY_LANGUAGE = Language(tspython.language())


@dataclass
class Symbol:
    """代码符号"""
    name: str
    kind: str           # "class", "function", "method"
    file_path: str
    line: int
    end_line: int
    parent: str | None  # 所属类名（方法时非 None）


def extract_symbols(file_path: str) -> list[Symbol]:
    """从 Python 文件中提取所有符号定义"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source.encode())
    symbols: list[Symbol] = []

    def visit(node, parent_class: str | None = None):
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                class_name = name_node.text.decode()
                symbols.append(Symbol(
                    name=class_name,
                    kind="class",
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    parent=parent_class,
                ))
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        visit(child, class_name)
            return

        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                symbols.append(Symbol(
                    name=name_node.text.decode(),
                    kind="method" if parent_class else "function",
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    parent=parent_class,
                ))
            return

        for child in node.children:
            visit(child, parent_class)

    visit(tree.root_node)
    return symbols


def find_references(file_path: str, symbol_name: str) -> list[tuple[int, str]]:
    """查找文件中对指定符号的引用（排除定义位置）"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
            lines = source.splitlines()
    except (OSError, UnicodeDecodeError):
        return []

    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source.encode())
    definitions = extract_symbols(file_path)
    def_lines = {s.line for s in definitions if s.name == symbol_name}

    refs: list[tuple[int, str]] = []

    def visit(node):
        if node.type == "identifier" and node.text.decode() == symbol_name:
            line = node.start_point[0] + 1
            if line not in def_lines:
                refs.append((line, lines[line - 1].rstrip()))
                return
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return refs


def collect_python_files(path: str, include: str | None = None) -> list[str]:
    """收集目录下的 Python 文件"""
    if os.path.isfile(path):
        return [path] if path.endswith(".py") else []

    files = []
    for root, _, filenames in os.walk(path):
        for fn in filenames:
            if include:
                ext = include.lstrip("*")
                if not fn.endswith(ext):
                    continue
            elif not fn.endswith(".py"):
                continue
            files.append(os.path.join(root, fn))
    return sorted(files)


class SymbolSearchTool(Tool):
    """语义级代码搜索工具"""

    @property
    def name(self) -> str:
        return "symbol_search"

    @property
    def description(self) -> str:
        return """语义级代码搜索。基于语法树解析，能区分定义和引用。

三种操作：
- definitions: 查找符号定义（函数、类、方法的声明位置）
- symbols: 列出文件或目录中的所有符号
- references: 查找符号被引用（非定义）的位置

比 grep 更精确：grep 搜 "call" 会匹配注释和字符串，
symbol_search 只匹配代码结构中的标识符。"""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["definitions", "symbols", "references"],
                    "description": "操作类型",
                },
                "name": {
                    "type": "string",
                    "description": "要搜索的符号名（definitions/references 时必需）",
                },
                "path": {
                    "type": "string",
                    "description": "搜索目录或文件（默认当前目录）",
                },
                "include": {
                    "type": "string",
                    "description": "文件过滤，如 *.py",
                },
            },
            "required": ["action"],
        }

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def call(self, input: dict[str, Any]) -> str:
        action = input.get("action", "")
        name = input.get("name", "")
        path = input.get("path", ".")
        include = input.get("include")

        if action == "definitions":
            return self._find_definitions(name, path, include)
        elif action == "symbols":
            return self._list_symbols(path, include)
        elif action == "references":
            return self._find_references(name, path, include)
        else:
            return f"未知操作: {action}。支持: definitions, symbols, references"

    def _find_definitions(self, name: str, path: str, include: str | None) -> str:
        if not name:
            return "错误：definitions 操作需要 name 参数"

        files = collect_python_files(path, include)
        matches: list[Symbol] = []
        for f in files:
            for sym in extract_symbols(f):
                if sym.name == name:
                    matches.append(sym)

        if not matches:
            return f"未找到符号定义: {name}"

        lines = [f"找到 {len(matches)} 处定义:", ""]
        for sym in matches:
            rel = os.path.relpath(sym.file_path)
            prefix = f"{sym.parent}." if sym.parent else ""
            lines.append(f"  {sym.kind} {prefix}{sym.name} ({rel}:{sym.line}-{sym.end_line})")

        return "\n".join(lines)

    def _list_symbols(self, path: str, include: str | None) -> str:
        files = collect_python_files(path, include)
        if not files:
            return f"未找到 Python 文件: {path}"

        output: list[str] = []
        for f in files:
            symbols = extract_symbols(f)
            if not symbols:
                continue
            rel = os.path.relpath(f)
            output.append(f"{rel}:")
            for sym in symbols:
                indent = "    " if sym.parent else "  "
                output.append(f"{indent}{sym.kind} {sym.name} ({sym.line}-{sym.end_line})")
            output.append("")

        if not output:
            return "未找到任何符号"

        return "\n".join(output)

    def _find_references(self, name: str, path: str, include: str | None) -> str:
        if not name:
            return "错误：references 操作需要 name 参数"

        files = collect_python_files(path, include)
        all_refs: list[tuple[str, int, str]] = []
        for f in files:
            for line_num, line_text in find_references(f, name):
                all_refs.append((f, line_num, line_text))

        if not all_refs:
            return f"未找到引用: {name}"

        lines = [f"找到 {len(all_refs)} 处引用:", ""]
        for file_path, line_num, line_text in all_refs:
            rel = os.path.relpath(file_path)
            lines.append(f"  {rel}:{line_num}: {line_text}")

        return "\n".join(lines)
