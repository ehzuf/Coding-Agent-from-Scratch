# 从零实现 Coding Agent（二十四）：Skill 自进化

上一篇实现了 Skills 系统——用户手动创建 Markdown 文件定义工作流，Agent 按需加载执行。但这有个问题：**技能完全靠人写**。如果 Agent 在复杂任务中摸索出了一套有效方法，下次遇到类似任务又得从头试。

本篇实现 **Skill 自进化**——让 Agent 在完成复杂任务后自动审查对话，把试错得来的经验沉淀为可复用的 Skill 文件。下次再遇到类似任务，Agent 可以直接调用这个 Skill，不用再重复试错。

## 核心思路

和第 21 篇的 Auto-Memory 一样，Skill 自进化也是"对话结束后，用 LLM 自己审查自己"。不同的是：

| | Auto-Memory | Skill 自进化 |
|---|---|---|
| 审查什么 | 用户偏好和项目事实 | 可复用的任务方法 |
| 触发条件 | 每次对话结束 | 工具调用次数达到阈值 |
| 输出格式 | 记忆条目（声明性知识） | SKILL.md（过程性知识） |
| 适用场景 | "记住用户喜欢 pathlib" | "记住 K8s Pod 排障的步骤" |

**声明性知识 vs 过程性知识**——这是关键区别。记忆是"知道什么"（facts），技能是"知道怎么做"（procedures）。

## 触发机制：什么时候该审查

不是每次对话都值得审查技能。只回答一个简单问题（1-2 轮工具调用）不会产生可复用的经验。只有**复杂任务**——经历了多轮工具调用、试错、调整——才可能产生值得沉淀的方法。

用一个计数器跟踪工具调用次数：

```python
SKILL_EVOLVE_THRESHOLD = 10  # 默认阈值：10 次工具调用
```

设计逻辑：
1. 每次 Agent 执行一轮工具调用，计数器 +1
2. 对话结束时，如果计数器 ≥ 阈值，触发技能审查
3. 审查完毕后计数器归零

阈值越小，审查越频繁（但 LLM 成本更高）。10 次是一个合理的默认值——意味着这次对话至少经历了 10 轮 LLM 决策+工具执行的循环，任务足够复杂。

## SkillEvolution 类

```python
"""
Skill 自进化 —— 对话结束后自动审查，将复杂任务的经验沉淀为 Skill

核心流程：
  1. 跟踪工具调用次数，达到阈值时触发审查
  2. 用 LLM 分析对话历史，判断是否有可复用的方法
  3. 有的话，自动创建或更新 SKILL.md 文件
  4. 下次对话时，新 Skill 被加载到 Agent 的工具集中
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from agent.llm.base import BaseLLM
from agent.skills import AGENT_HOME, SKILLS_DIRNAME, _parse_frontmatter


SKILL_EVOLVE_THRESHOLD = 10

SKILLS_DIR = os.path.join(AGENT_HOME, SKILLS_DIRNAME)
```

`SKILLS_DIR` 指向 `~/.coding-agent/skills/`，和第 23 篇的 Skill 加载目录一致。自动生成的 Skill 放在这里，下次启动时 `load_skills()` 自然能发现它们。

## 审查 Prompt：告诉 LLM 找什么

审查 Prompt 是整个机制的核心——它定义了 LLM 的"审查视角"：

````python
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
````

Prompt 里的三个判断维度——试错、转向、用户纠正——直接对应了 Hermes Agent 的 `_SKILL_REVIEW_PROMPT`。这不是随意设计的，它抓住了"经验"的本质：**不是任何操作都值得记住，只有那些"走弯路后找到正确路"的经验才有复用价值**。

`{existing_skills}` 占位符会在运行时替换为当前已有的 Skill 列表，让 LLM 能做去重判断——已有 Skill 覆盖的场景不需要重复创建，但可以更新（进化）。

## 对话摘要构建

和 Auto-Memory 一样，需要把对话历史压缩成 LLM 能处理的摘要：

```python
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
```

注意两个设计选择：
- **工具结果截断为 200 字符**：完整的工具输出可能很长（比如 `grep` 结果），但审查只需要知道"做了什么"和"大致结果"
- **总长度上限 20000 字符**：约 6000-7000 tokens，留足空间给审查 LLM 的推理和输出

## 已有 Skill 清单

审查前需要告诉 LLM 当前已有哪些 Skill，避免重复创建：

```python
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
```

复用了第 23 篇的 `_parse_frontmatter()` 来读取 frontmatter。只提取 name 和 description——审查 LLM 只需要知道"有个叫 code-review 的 Skill，做代码审查"就够了，不需要看完整内容。

## LLM 审查结果解析

LLM 返回的 JSON 可能被 markdown 代码块包裹，需要稳健的解析：

```python
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
```

过滤条件确保每个结果都有必要字段。`action` 必须是 `create` 或 `update`——这限制了 LLM 只能做这两种操作，不会出现意外行为。

## Skill 文件写入

创建新 Skill 或更新已有 Skill 的文件操作：

```python
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
```

几个安全措施：
- **路径穿越防护**：`name` 不能包含 `/`、`\`、`..`
- **frontmatter 验证**：必须有正文内容，纯 frontmatter 的空 Skill 没有意义
- **create 不覆盖**：已存在同名 Skill 时跳过，防止意外覆盖手动创建的 Skill
- **update 要求已存在**：只更新已有的 Skill，不会凭空创建

> **与 Hermes Agent 的差异**：Hermes 使用原子写入（临时文件 + `os.replace`）防止写到一半崩溃导致文件损坏，还有 `skills_guard` 模块对每个新 Skill 做安全扫描（检测 Prompt 注入等）。我们简化了这两个环节——教学项目中 Skill 内容由受信任的 LLM 生成，崩溃保护和安全扫描在理解核心机制后可以自行添加。

## SkillEvolution 核心类

把上面的组件串起来：

```python
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
    _counter: int = 0

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
```

`_do_review()` 的流程和 Auto-Memory 的 `extract_and_save()` 非常相似：构建摘要 → 准备 prompt → 调用 LLM → 解析结果 → 保存文件。这不是巧合——两者本质上都是"用 LLM 审查对话，提取持久化信息"，只是提取的内容类型不同。

注意 `review_messages` 的构造：先放对话摘要（作为 user 消息），再放一个 assistant 确认（"我已阅读"），最后放审查 prompt。这种三条消息的结构让 LLM 能清晰区分"对话内容"和"审查指令"。

> **与 Hermes Agent 的差异**：Hermes 不是直接调用 `llm.chat()`，而是 fork 了一个**完整的 Agent 实例**（拥有 `skill_manage` 工具），在后台 daemon 线程中运行。审查 Agent 会自己判断要 create 还是 patch，然后调用工具执行。我们简化为直接 LLM 调用 + JSON 输出，核心逻辑一样，但省去了 fork Agent、线程管理、工具调用循环等复杂度。

## 集成到 Agent

### 构造函数

在 Agent 中加入 `skill_evolution` 参数：

```python
class Agent:
    def __init__(
        self,
        ...,
        skill_evolution: SkillEvolution | None = None,
    ):
        ...
        self.skill_evolution = skill_evolution
```

### 工具调用时 tick

在 Tool Use 循环中，每轮工具执行完后递增计数器：

```python
# _run_tool_loop 中，执行完工具后：
if self.skill_evolution:
    self.skill_evolution.tick()
```

放在工具**执行完**之后，而不是执行之前。因为计数器统计的是"Agent 实际做了多少事"，而不是"LLM 请求了多少次工具"。

### 主动调用 skill 工具时重置

如果 Agent 在对话中主动调用了 `skill` 工具（手动执行 Skill），说明技能相关的操作已经发生了，不需要后台再审查：

```python
# _execute_tool 中，执行完 skill 工具后：
if tool_name == "skill" and self.skill_evolution:
    self.skill_evolution.reset()
```

### 对话结束时审查

在 LLM 最终回复（无工具调用）后触发：

```python
# 如果没有工具调用，返回结果
if not response.has_tool_use:
    self._maybe_extract_memories()   # Auto-Memory
    self._maybe_evolve_skills()       # Skill 自进化（新增）
    return response
```

```python
def _maybe_evolve_skills(self) -> None:
    """对话结束时尝试进化 Skill。静默执行，失败不影响主流程。"""
    if not self.skill_evolution:
        return
    try:
        results = self.skill_evolution.maybe_evolve(self.messages)
        for r in results:
            print(f"  💡 {r}")
    except Exception:
        pass
```

和 Auto-Memory 一样，`_maybe_evolve_skills()` 用 try/except 包裹，任何异常都被静默吞掉。这是 best-effort 设计——Skill 自进化是锦上添花，不能因为它出错而影响用户的正常交互。

成功时打印 `💡 Skill 'xxx' 已创建`，让用户知道发生了什么。

### build_agent 集成

```python
from agent.skill_evolution import SkillEvolution

# Skill 自进化
skill_evolution = SkillEvolution(llm=llm)

agent = Agent(
    ...,
    skill_evolution=skill_evolution,
)
```

## 完整数据流

```
用户提问
  ├→ Agent 对话循环
  │    ├→ LLM 决策 → 执行工具 → skill_evolution.tick()
  │    ├→ LLM 决策 → 执行工具 → skill_evolution.tick()
  │    ├→ ...（重复多轮）
  │    └→ LLM 最终回复（无工具调用）
  │         ├→ _maybe_extract_memories()      # 记忆提取
  │         └→ _maybe_evolve_skills()          # 技能审查
  │              ├→ counter < 10? → 跳过
  │              └→ counter >= 10? → 触发审查
  │                   ├→ 构建对话摘要
  │                   ├→ 扫描已有 Skill
  │                   ├→ LLM 分析 → JSON 结果
  │                   ├→ 创建/更新 SKILL.md
  │                   └→ 打印 "💡 Skill 'xxx' 已创建"
  ↓
下次对话启动
  └→ load_skills() 扫描 ~/.coding-agent/skills/
       └→ 新的 Skill 被发现 → 注册到 SkillTool → Agent 可以调用
```

## REPL 命令

新增 `/evolve` 命令，强制触发一次技能审查（不受阈值限制，方便调试）：

```python
if prompt == "/evolve":
    if agent.skill_evolution:
        # 临时设置计数器为阈值，强制触发
        agent.skill_evolution._counter = agent.skill_evolution.threshold
        results = agent.skill_evolution.maybe_evolve(agent.messages)
        if results:
            for r in results:
                print(f"  💡 {r}")
        else:
            print("\n当前对话没有值得沉淀的 Skill。\n")
    else:
        print("\nSkill 自进化未启用。\n")
    continue
```

## 进化效果示例

假设用户让 Agent 排查一个 K8s Pod 反复重启的问题。Agent 经历了：
1. 先看 events → 没有有用信息
2. 改看 logs → 发现 OOMKilled
3. 查 resource limits → 发现内存配置过低
4. 修改 deployment → 问题解决

这次对话经历了 12+ 轮工具调用，触发了技能审查。审查 LLM 分析后输出：

```json
[
  {
    "action": "create",
    "name": "k8s-pod-restart-debug",
    "description": "排查 K8s Pod 反复重启的问题",
    "content": "---\nname: k8s-pod-restart-debug\ndescription: 排查 K8s Pod 反复重启的问题\nallowed_tools: [bash, read]\n---\n\n排查 Pod 反复重启时，按以下顺序检查：\n\n1. 查看 Pod 状态和重启原因：`kubectl describe pod <name>`\n2. 检查容器退出码（OOMKilled=137, Error=1）\n3. 如果 OOMKilled：检查 resource limits 和实际内存使用\n4. 如果 Error：查看容器日志 `kubectl logs <pod> --previous`\n5. 检查 liveness/readiness probe 配置\n\n常见陷阱：\n- events 会被清理，别只看 events\n- 用 --previous 看上一次崩溃的日志，不是当前容器的\n"
  }
]
```

下次用户再遇到 Pod 重启问题时，Agent 会发现已有 `k8s-pod-restart-debug` Skill，直接按步骤排查——第一步就看 Pod 状态和退出码，不会再走"先看 events → 没信息 → 改看 logs"的弯路。

这就是"进化"的含义：**把试错经验固化为可复用的流程**。

## 与 Hermes Agent 的对比

| 方面 | Hermes Agent | 我们的实现 |
|---|---|---|
| 触发机制 | 工具迭代计数器（默认 10） | 同样 |
| 审查方式 | fork 完整 Agent + daemon 线程 | 直接 llm.chat()（简化） |
| 判断逻辑 | LLM 自行决定 create/patch | 同样（通过 JSON action 字段） |
| Skill 更新 | patch（find-and-replace） | 全文覆盖（简化） |
| 安全扫描 | skills_guard 模块扫描 Prompt 注入 | 无（教学简化） |
| 原子写入 | 临时文件 + os.replace | 直接 write_text（简化） |
| 用户通知 | 💾 摘要 + 网关回调 | 💡 终端打印 |
| 递归保护 | 审查 Agent 的 nudge_interval=0 | 无需（没有 fork Agent） |

Hermes 的 fork Agent 方案更强大——审查 Agent 拥有完整工具集，可以先用 `skill_view()` 查看已有 Skill 的完整内容，再决定是 patch 特定段落还是整体重写。但理解成本也更高。我们的简化方案捕获了核心理念，可以在此基础上逐步增强。

## 设计回顾

Skill 自进化的核心洞察是：**让 LLM 审查 LLM 自己的工作**。

```
对话过程中                        对话结束后
┌──────────────┐              ┌──────────────┐
│  主 Agent    │              │  审查 LLM    │
│  执行任务    │  ──快照──▶   │  分析对话    │
│  试错探索    │              │  提取经验    │
└──────────────┘              └──────┬───────┘
                                     │
                              ┌──────▼───────┐
                              │  SKILL.md    │
                              │  持久化文件   │
                              └──────┬───────┘
                                     │
下次对话                              │
┌──────────────┐                     │
│  主 Agent    │  ◀──加载──────────────┘
│  有了经验    │
│  不再试错    │
└──────────────┘
```

这不是强化学习——没有 reward model、没有 policy gradient、不修改模型权重。它更像是一种**外部化的经验记忆**：用 LLM 的语义理解能力来识别"什么值得记"，用文件系统来实现"记住"，用 system prompt 注入来实现"想起来"。

至此，教程系列覆盖了一个完整 Coding Agent 从零到一的所有核心能力。Skill 自进化是最后一块拼图——它让 Agent 从"每次从零开始"变成"越用越聪明"。
