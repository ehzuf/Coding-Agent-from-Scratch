"""
Skill 自进化 —— 对话结束后自动审查，将复杂任务的经验沉淀为 Skill

对应 Hermes Agent 中 run_agent.py 的 _spawn_background_review() 机制。
Hermes 使用 fork 完整 Agent + daemon 线程做后台审查，
我们简化为直接 llm.chat() 调用，核心逻辑一致。

核心流程：
  1. 跟踪工具调用次数，达到阈值时触发审查
  2. 用 LLM 分析对话历史，判断是否有可复用的方法
  3. 有的话，自动创建或更新 SKILL.md 文件
  4. 下次对话时，新 Skill 被加载到 Agent 的工具集中
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.llm.base import BaseLLM
from agent.skills import AGENT_HOME, SKILLS_DIRNAME, _parse_frontmatter


# ============================================================================
# 配置
# ============================================================================

SKILL_EVOLVE_THRESHOLD = 10

SKILLS_DIR = os.path.join(AGENT_HOME, SKILLS_DIRNAME)


# ============================================================================
# 审查 Prompt
# ============================================================================

REVIEW_PROMPT = """分析上面的对话历史，判断是否有值得保存为 Skill 的可复用方法。

## 什么值得保存

关注以下情况：
1. **经过试错才找到的方法** —— 不是一步到位，中间经历了 trial and error
2. **中途调整了策略** —— 因为发现新情况而改变了做法
3. **用户纠正后的方法** —— 用户指出了更好的做法

## 什么不值得保存

- 简单的一问一答
- 只用了 1-2 个工具的简单操作
- 已有 Skill 完全覆盖的场景

## 已有 Skill

{existing_skills}

如果新发现的方法属于已有 Skill 的范畴，输出 update 类型。如果是全新的，输出 create 类型。

## 输出格式

如果有值得保存的 Skill，输出 JSON：

```json
[
  {{
    "action": "create 或 update",
    "name": "skill-name（小写字母、连字符）",
    "description": "一句话描述",
    "content": "完整的 SKILL.md 内容（含 frontmatter + 正文）"
  }}
]
```

如果没有值得保存的内容，输出空数组 `[]`。
"""


# ============================================================================
# 辅助函数
# ============================================================================

def _build_conversation_summary(messages: list[dict], max_chars: int = 20000) -> str:
    """
    构建对话摘要。

    将消息历史压缩为文本，工具调用和结果做简化处理，
    控制总长度不超过 max_chars。
    """
    parts = []
    total = 0

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        texts.append(f"[工具调用: {block.get('name', '')}]")
                    elif block.get("type") == "tool_result":
                        result = block.get("content", "")
                        if isinstance(result, str) and len(result) > 200:
                            result = result[:200] + "..."
                        texts.append(f"[工具结果: {result}]")
            text = "\n".join(texts)
        else:
            text = str(content)

        # 单条消息截断
        if len(text) > 1500:
            text = text[:1500] + "..."

        line = f"[{role}] {text}"
        if total + len(line) > max_chars:
            break
        parts.append(line)
        total += len(line)

    return "\n\n".join(parts)


def _build_skill_manifest(skills_dir: str) -> str:
    """
    扫描 skills 目录，构建已有 Skill 的清单。

    输出格式：
      - code-review: 审查代码变更并给出建议
      - k8s-troubleshoot: K8s Pod 排障流程
    """
    if not os.path.isdir(skills_dir):
        return "（暂无已有 Skill）"

    lines = []
    for entry in sorted(os.listdir(skills_dir)):
        skill_md = os.path.join(skills_dir, entry, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        try:
            content = Path(skill_md).read_text(encoding="utf-8")
            fm, _ = _parse_frontmatter(content)
            name = fm.get("name", entry)
            desc = fm.get("description", "")
            lines.append(f"- {name}: {desc}" if desc else f"- {name}")
        except Exception:
            lines.append(f"- {entry}")

    return "\n".join(lines) if lines else "（暂无已有 Skill）"


def _parse_review_result(text: str) -> list[dict]:
    """
    解析审查 LLM 返回的 JSON 结果。

    支持裸 JSON 和 markdown 代码块包裹两种格式。
    返回解析后的 Skill 列表，解析失败返回空列表。
    """
    # 尝试从 markdown 代码块中提取
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    json_str = match.group(1) if match else text

    try:
        result = json.loads(json_str)
        if isinstance(result, list):
            return [
                item for item in result
                if isinstance(item, dict)
                and item.get("name")
                and item.get("content")
                and item.get("action") in ("create", "update")
            ]
        return []
    except (json.JSONDecodeError, ValueError):
        return []


def _save_skill(skill: dict, skills_dir: str) -> str | None:
    """
    保存 Skill 到文件系统。

    create 操作：创建新目录和 SKILL.md
    update 操作：覆盖已有 SKILL.md

    Returns:
        成功返回操作描述，失败返回 None
    """
    action = skill.get("action", "create")
    name = skill.get("name", "")
    content = skill.get("content", "")

    if not name or not content:
        return None

    # 安全检查：名称不能包含路径分隔符
    if "/" in name or "\\" in name or ".." in name:
        return None

    skill_dir = os.path.join(skills_dir, name)
    skill_md = os.path.join(skill_dir, "SKILL.md")

    # 验证 content 有合法的 frontmatter
    fm, body = _parse_frontmatter(content)
    if not body:
        return None

    if action == "create":
        # 已存在同名 Skill，跳过
        if os.path.exists(skill_dir):
            return None
        os.makedirs(skill_dir, exist_ok=True)
        Path(skill_md).write_text(content, encoding="utf-8")
        return f"Skill '{name}' 已创建"

    elif action == "update":
        # 不存在则跳过
        if not os.path.isfile(skill_md):
            return None
        Path(skill_md).write_text(content, encoding="utf-8")
        return f"Skill '{name}' 已更新"

    return None


# ============================================================================
# SkillEvolution 核心类
# ============================================================================

@dataclass
class SkillEvolution:
    """
    Skill 自进化管理器。

    跟踪工具调用次数，对话结束时触发审查，
    自动创建或更新 Skill 文件。

    用法：
        evo = SkillEvolution(llm=llm)

        # 每次工具调用后
        evo.tick()

        # 对话结束时
        results = evo.maybe_evolve(messages)
    """
    llm: BaseLLM
    threshold: int = SKILL_EVOLVE_THRESHOLD
    _counter: int = field(default=0, repr=False)

    def tick(self) -> None:
        """记录一次工具调用迭代。每次 Agent 执行完一轮工具后调用。"""
        self._counter += 1

    def reset(self) -> None:
        """重置计数器。Agent 主动调用 skill 工具时调用，跳过后续审查。"""
        self._counter = 0

    def should_review(self) -> bool:
        """判断是否应该触发技能审查。"""
        return self._counter >= self.threshold

    def maybe_evolve(self, messages: list[dict]) -> list[str]:
        """
        对话结束时调用。如果达到审查条件，分析对话并沉淀 Skill。

        Args:
            messages: 当前对话的完整消息历史

        Returns:
            操作结果列表（如 ["Skill 'k8s-debug' 已创建"]），
            未触发或无结果时返回空列表
        """
        if not self.should_review():
            return []

        # 重置计数器
        self._counter = 0

        # 对话太短不审查
        if len(messages) < 4:
            return []

        try:
            return self._do_review(messages)
        except Exception:
            return []

    def _do_review(self, messages: list[dict]) -> list[str]:
        """执行审查流程。"""
        # 1. 构建对话摘要
        summary = _build_conversation_summary(messages)

        # 2. 构建已有 Skill 清单
        existing = _build_skill_manifest(SKILLS_DIR)

        # 3. 构建审查 prompt
        prompt = REVIEW_PROMPT.format(existing_skills=existing)

        # 4. 调用 LLM 审查
        review_messages = [
            {"role": "user", "content": summary},
            {"role": "assistant", "content": "我已阅读对话历史，准备审查是否有可复用的方法。"},
            {"role": "user", "content": prompt},
        ]
        response = self.llm.chat(review_messages)

        # 5. 解析结果
        skills = _parse_review_result(response.text)
        if not skills:
            return []

        # 6. 保存 Skill 文件
        results = []
        for skill in skills:
            msg = _save_skill(skill, SKILLS_DIR)
            if msg:
                results.append(msg)

        return results
