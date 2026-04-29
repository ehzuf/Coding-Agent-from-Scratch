# 从零实现 Coding Agent（六）：System Prompt + AGENTS.md

前几篇文章，我们实现了 LLM 调用、流式输出、多轮对话、Tool Use 和文件操作工具。现在，让我们给 agent 添加"项目感知"能力——通过 AGENTS.md 文件。

## 为什么需要 AGENTS.md？

想象你在一个陌生的代码库工作，你需要了解：
- 项目使用什么技术栈？
- 代码风格有什么约定？
- 文件结构是怎样的？
- 有哪些开发规范？

LLM 面临同样的问题。每次对话都是独立的，它不知道当前项目的背景信息。AGENTS.md 就是解决这个问题的——一个放在项目根目录的规范文件，agent 会自动读取并融入 system prompt。

## AGENTS.md 的设计

AGENTS.md 的灵感来自：
- **.cursorrules**（Cursor 编辑器的项目规则）
- **CLAUDE.md**（Claude Code 的配置文件）
- **CONTRIBUTING.md**（开源项目的贡献指南）

> **教学简化**：Claude Code 使用 `CLAUDE.md` 作为文件名，且支持 4 层记忆结构（Managed/User/Project/Local）。我们这里简化为单个 `AGENTS.md` 文件 + 向上递归查找，后续教程（第十八篇）会扩展为完整的分层结构。

它应该包含：
1. **技术栈**：Python 版本、框架、库
2. **代码风格**：命名规范、格式化工具
3. **项目结构**：目录组织、关键文件
4. **开发约定**：测试要求、提交规范

## 实现 AGENTS.md 加载

创建 `agent/context.py`：

```python
"""
上下文管理 —— 加载 AGENTS.md 和系统上下文
"""

import os
from datetime import datetime
from pathlib import Path


def find_agents_md(start_dir: str = ".") -> str | None:
    """
    从指定目录向上递归查找 AGENTS.md 文件。

    查找顺序：start_dir → parent → grandparent → ... → root
    """
    current = Path(start_dir).resolve()

    while True:
        agents_file = current / "AGENTS.md"
        if agents_file.exists() and agents_file.is_file():
            return str(agents_file)

        # 到达根目录，停止查找
        parent = current.parent
        if parent == current:
            break
        current = parent

    return None


def load_agents_md(start_dir: str = ".") -> str | None:
    """加载 AGENTS.md 文件内容。"""
    file_path = find_agents_md(start_dir)
    if file_path is None:
        return None

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None
```

向上递归查找的设计很重要——这允许你在子目录运行 agent，仍然能找到项目根目录的 AGENTS.md。

## 构建完整 System Prompt

AGENTS.md 只是 system prompt 的一部分。我们还需要注入其他上下文：

```python
def build_system_prompt(
    base_system: str | None = None,
    cwd: str = ".",
    include_date: bool = True,
) -> str | None:
    """
    构建完整的系统提示。

    包含：
      1. 基础系统提示
      2. AGENTS.md 内容
      3. 当前日期时间
      4. 工作目录
    """
    parts = []

    # 1. 基础系统提示
    if base_system:
        parts.append(base_system)

    # 2. AGENTS.md 内容
    agents_content = load_agents_md(cwd)
    if agents_content:
        parts.append("## 项目规范 (AGENTS.md)\n")
        parts.append(agents_content)

    # 3. 上下文信息
    context_parts = []

    if include_date:
        now = datetime.now()
        context_parts.append(f"当前日期时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")

    abs_cwd = os.path.abspath(cwd)
    context_parts.append(f"工作目录: {abs_cwd}")

    if context_parts:
        parts.append("## 上下文信息\n")
        parts.append("\n".join(context_parts))

    if not parts:
        return None

    return "\n\n".join(parts)


def get_context_info(cwd: str = ".") -> dict:
    """返回当前上下文的概要信息，用于 CLI 显示。"""
    abs_cwd = os.path.abspath(cwd)
    agents_path = find_agents_md(cwd)
    return {
        "cwd": abs_cwd,
        "agents_md_loaded": agents_path is not None,
        "agents_md_path": str(agents_path) if agents_path else None,
    }
```

## 在 CLI 中使用

修改 `__main__.py`，在构建 Agent 时加载 AGENTS.md：

```python
from agent.context import build_system_prompt, get_context_info

def build_agent(args) -> Agent:
    """根据命令行参数构建 Agent 实例。"""
    # ... 初始化 LLM ...

    # 构建系统提示
    system_prompt = build_system_prompt(
        base_system=args.system,
        cwd=args.cwd,
        include_date=True,
    )

    # 显示上下文信息
    context_info = get_context_info(args.cwd)
    print(f"工作目录: {context_info['cwd']}")
    if context_info["agents_md_loaded"]:
        print(f"已加载 AGENTS.md: {context_info['agents_md_path']}")

    return Agent(llm=llm, tools=tools, system=system_prompt)
```

添加 CLI 参数：

```python
parser.add_argument(
    "--cwd",
    default=".",
    help="工作目录（默认当前目录），用于查找 AGENTS.md",
)
parser.add_argument(
    "--system",
    default=None,
    help="额外的系统提示，会与 AGENTS.md 内容合并",
)
```

## 使用示例

创建一个示例 AGENTS.md：

```markdown
# MyProject 规范

## 技术栈
- Python 3.10+
- FastAPI + SQLAlchemy
- pytest 用于测试

## 代码风格
- 使用 black 格式化
- 类型注解是必需的
- 函数文档字符串使用 Google 风格

## 项目结构
```
myproject/
├── app/           # 应用代码
├── tests/         # 测试文件
├── alembic/       # 数据库迁移
└── docs/          # 文档
```

## 开发约定
- 所有 API 端点必须有测试
- 数据库模型变更需要迁移
- 提交前运行 `make lint`
```

运行 agent：

```bash
$ python -m agent "这个项目的测试怎么写？"

[anthropic / claude-sonnet-4-20250514] 工作目录: ~/myproject
  已加载 AGENTS.md: ~/myproject/AGENTS.md
  可用工具: [..., read, write, edit, ...]

根据项目规范，测试使用 pytest 编写。所有 API 端点都必须有测试...
```

Agent 自动了解了项目的技术栈和测试要求。

## 从子目录运行

向上递归查找的优势：

```bash
$ cd myproject/app/routers
$ python -m agent "查看用户相关的路由"

工作目录: ~/myproject/app/routers
  已加载 AGENTS.md: ~/myproject/AGENTS.md
```

即使你在子目录，agent 也能找到项目根目录的 AGENTS.md。

## 动态上下文

除了 AGENTS.md，我们还注入了动态上下文：

1. **当前日期时间**：让 agent 知道"现在"是什么时候
2. **工作目录**：让 agent 知道当前位置

这些信息帮助 agent 做出更合适的决策。例如：
- 知道日期可以正确解释时间相关的代码
- 知道工作目录可以正确处理相对路径

## 这一步我们学到了什么

1. **项目上下文很重要**：LLM 需要了解项目背景才能给出合适的建议
2. **AGENTS.md 是项目规范载体**：类似 CONTRIBUTING.md，但面向 agent
3. **向上递归查找**：允许在任意子目录运行，仍能加载项目配置
4. **动态信息注入**：日期、工作目录等上下文增强 agent 的感知能力

下一篇文章，我们将实现 **Tool Result Budget**——限制工具输出的长度，防止大输出撑爆上下文窗口。
