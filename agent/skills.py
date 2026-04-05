"""
Skills 系统 —— 自定义技能（markdown prompt + frontmatter）

对应 reference 中的 skills/loadSkillsDir.ts + tools/SkillTool/

核心设计：
  - Skill 定义：markdown 文件 + frontmatter
  - Skill 来源：用户级 ~/.coding-agent/skills/ + 项目级 .coding-agent/skills/
  - SkillTool：Agent 通过工具调用执行 Skill（fork 子 Agent）
  - Frontmatter 字段：name, description, allowed_tools, context（inline/fork）

Skill 文件格式示例（~/.coding-agent/skills/code-review.md）：
  ---
  name: code-review
  description: 审查代码变更并给出建议
  allowed_tools: [read, glob, grep]
  context: fork
  ---

  请审查以下代码变更，关注：
  1. 正确性
  2. 安全性
  3. 性能
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.tools.base import Tool


# ============================================================================
# 配置
# ============================================================================

AGENT_HOME = os.path.expanduser("~/.coding-agent")
SKILLS_DIRNAME = "skills"


# ============================================================================
# Skill 数据结构
# ============================================================================

@dataclass
class SkillDefinition:
    """一个 Skill 的完整定义。"""
    name: str
    description: str
    prompt: str  # frontmatter 之后的 markdown 内容
    source: str  # 来源路径
    allowed_tools: list[str] = field(default_factory=list)
    context: str = "fork"  # "inline" 或 "fork"


# ============================================================================
# Frontmatter 解析
# ============================================================================

def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """
    解析 markdown frontmatter，返回 (frontmatter_dict, body)。

    frontmatter 是 --- 包围的 YAML-like 内容。
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    if not match:
        return {}, content

    fm_text = match.group(1)
    body = match.group(2).strip()

    data = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()

            # 解析列表格式 [a, b, c]
            if value.startswith("[") and value.endswith("]"):
                items = value[1:-1].split(",")
                data[key] = [item.strip() for item in items if item.strip()]
            else:
                data[key] = value

    return data, body


# ============================================================================
# Skill 加载
# ============================================================================

def load_skills(cwd: str = ".") -> list[SkillDefinition]:
    """
    从所有来源加载 Skill 定义。

    来源（按优先级）：
      1. 用户级：~/.coding-agent/skills/*.md
      2. 项目级：.coding-agent/skills/*.md（同名覆盖用户级）

    Returns:
        SkillDefinition 列表
    """
    skills: dict[str, SkillDefinition] = {}

    # 用户级
    user_dir = os.path.join(AGENT_HOME, SKILLS_DIRNAME)
    _load_skills_from_dir(user_dir, "user", skills)

    # 项目级（覆盖同名）
    project_dir = os.path.join(cwd, ".coding-agent", SKILLS_DIRNAME)
    _load_skills_from_dir(project_dir, "project", skills)

    return list(skills.values())


def _load_skills_from_dir(
    directory: str,
    source_type: str,
    skills: dict[str, SkillDefinition],
) -> None:
    """从目录加载 Skill 文件。支持两种格式：
    - 文件格式：skills/name.md
    - 目录格式：skills/name/SKILL.md（Claude Code 格式）
    """
    if not os.path.isdir(directory):
        return

    for entry in sorted(os.listdir(directory)):
        entry_path = os.path.join(directory, entry)

        # 目录格式：skills/name/SKILL.md
        if os.path.isdir(entry_path):
            skill_file = os.path.join(entry_path, "SKILL.md")
            if os.path.isfile(skill_file):
                _load_single_skill(skill_file, entry, source_type, skills)
            continue

        # 文件格式：skills/name.md
        if not entry.endswith(".md"):
            continue
        if not os.path.isfile(entry_path):
            continue

        default_name = entry.replace(".md", "")
        _load_single_skill(entry_path, default_name, source_type, skills)


def _load_single_skill(
    filepath: str,
    default_name: str,
    source_type: str,
    skills: dict[str, SkillDefinition],
) -> None:
    """加载单个 Skill 文件。"""
    try:
        content = Path(filepath).read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(content)

        if not body:
            return

        # 从 frontmatter 或文件名推断 name
        name = fm.get("name", default_name)
        description = fm.get("description", "")
        allowed_tools = fm.get("allowed_tools", [])
        if isinstance(allowed_tools, str):
            allowed_tools = [allowed_tools]
        context = fm.get("context", "fork")

        skill = SkillDefinition(
            name=name,
            description=description,
            prompt=body,
            source=f"[{source_type}] {filepath}",
            allowed_tools=allowed_tools,
            context=context,
        )
        skills[name] = skill

    except Exception:
        pass


# ============================================================================
# SkillTool —— Agent 工具：执行 Skill
# ============================================================================

class SkillTool(Tool):
    """
    Skill 执行工具。

    Agent 通过 tool_use 调用此工具来执行已注册的 Skill。
    执行方式：fork 一个子 Agent，使用 Skill 的 prompt 作为系统提示。
    """

    def __init__(
        self,
        skills: list[SkillDefinition],
        llm: object,  # BaseLLM
        parent_tools: list[Tool],
        max_turns: int = 10,
    ):
        self._skills = {s.name: s for s in skills}
        self._llm = llm
        self._parent_tools = parent_tools
        self._max_turns = max_turns

    @property
    def name(self) -> str:
        return "skill"

    @property
    def description(self) -> str:
        if not self._skills:
            return "执行自定义 Skill（暂无可用 Skill）"

        skill_list = ", ".join(
            f"{s.name}（{s.description}）" if s.description else s.name
            for s in self._skills.values()
        )
        return f"执行自定义 Skill。可用 Skill: {skill_list}"

    @property
    def input_schema(self) -> dict:
        skill_names = list(self._skills.keys())
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": f"Skill 名称。可选: {skill_names}",
                },
                "args": {
                    "type": "string",
                    "description": "传递给 Skill 的参数（可选）",
                },
            },
            "required": ["name"],
        }

    def call(self, tool_input: dict) -> str:
        """执行指定的 Skill。"""
        skill_name = tool_input.get("name", "")
        args = tool_input.get("args", "")

        skill = self._skills.get(skill_name)
        if not skill:
            available = list(self._skills.keys())
            return f"错误：未知 Skill '{skill_name}'。可用: {available}"

        # 构建 Skill prompt
        prompt = skill.prompt
        if args:
            prompt = f"{prompt}\n\n用户参数: {args}"

        # 选择可用工具
        if skill.allowed_tools:
            sub_tools = [
                t for t in self._parent_tools
                if t.name in skill.allowed_tools
            ]
        else:
            sub_tools = [
                t for t in self._parent_tools
                if t.name not in ("skill", "agent", "send_message",
                                  "enter_plan_mode", "exit_plan_mode")
            ]

        # Fork 子 Agent 执行
        try:
            from agent.agent import Agent

            sub_agent = Agent(
                llm=self._llm,
                tools=sub_tools,
                system=f"你正在执行 Skill: {skill.name}\n\n{skill.description}" if skill.description else None,
                max_turns=self._max_turns,
                enable_compact=False,
                enable_permission=False,
                _enable_agent_tool=False,
            )

            response = sub_agent.chat(prompt)
            result_text = response.text or "(Skill 未返回文本)"

            return (
                f"[Skill: {skill.name}]\n"
                f"{result_text}\n"
                f"--- Skill 执行完成 (turns={sub_agent.turn_count}) ---"
            )

        except Exception as e:
            return f"Skill '{skill.name}' 执行失败: {e}"

    def is_concurrency_safe(self, tool_input: dict) -> bool:
        """Skill 默认不并发。"""
        return False


# ============================================================================
# SkillManager —— 管理 Skill 加载和注册
# ============================================================================

class SkillManager:
    """
    Skill 管理器。

    加载、注册和管理 Skill 定义。

    用法：
        manager = SkillManager(cwd="/path/to/project")
        skills = manager.skills
        skill_tool = manager.create_skill_tool(llm, tools)
    """

    def __init__(self, cwd: str = "."):
        self.cwd = cwd
        self.skills = load_skills(cwd)

    def has_skills(self) -> bool:
        """是否有可用的 Skill。"""
        return len(self.skills) > 0

    def create_skill_tool(
        self,
        llm: object,
        parent_tools: list[Tool],
        max_turns: int = 10,
    ) -> SkillTool:
        """创建 SkillTool 实例。"""
        return SkillTool(
            skills=self.skills,
            llm=llm,
            parent_tools=parent_tools,
            max_turns=max_turns,
        )

    def get_summary(self) -> str:
        """获取 Skill 摘要信息。"""
        if not self.skills:
            return "暂无可用 Skill"

        lines = ["可用 Skill:"]
        for s in self.skills:
            desc = f" — {s.description}" if s.description else ""
            tools = f" (工具: {s.allowed_tools})" if s.allowed_tools else ""
            lines.append(f"  {s.name}{desc}{tools}")
            lines.append(f"    来源: {s.source}")

        return "\n".join(lines)

    def list_skill_names(self) -> list[str]:
        """列出所有 Skill 名称。"""
        return [s.name for s in self.skills]
