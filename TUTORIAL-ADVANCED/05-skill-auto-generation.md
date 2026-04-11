# 从 Coding Agent 到个人助理（五）：技能自生成

前面的系列中我们实现了 Skills 系统——用户手动创建 Markdown 文件定义可复用工作流。但一个更有意思的问题是：**Agent 能不能自己创建 Skill？**

Hermes Agent 的答案是"能"。它的做法出人意料地简单——完成一轮对话后，fork 一个后台 Agent 回顾刚才的对话，自行判断有没有值得保存的经验。

本篇拆解这个机制的完整实现。

 
## 核心思路

技能自生成**不是什么复杂的 AI 自学习系统**，它的本质就一句话：

> 每隔 N 次工具调用，让一个后台 Agent 看完对话记录，决定要不要存个 Skill。

这个设计背后的洞察是：LLM 本身就有足够的判断力来识别"什么经验值得记住"。我们不需要额外的机制，只需要给它机会去回顾。

## 触发机制

### 迭代计数器

每次主 Agent 执行一个工具调用，计数器加 1：

```python
# agent/agent.py

class Agent:
    def __init__(self, config):
        # ... 原有初始化 ...
        self._iters_since_skill = 0
        self._skill_nudge_interval = config.get("skill_nudge_interval", 10)
```

在工具执行循环中递增：

```python
def _execute_tool(self, tool_call):
    # 执行工具...
    result = tool.execute(**args)
    
    # 递增计数器
    self._iters_since_skill += 1
    
    return result
```

### 对话结束后检查

一轮对话完成后，检查是否达到阈值：

```python
def chat(self, user_message):
    # ... 正常的对话 + 工具调用循环 ...
    
    # 对话结束后，检查是否触发后台审查
    if (self._skill_nudge_interval > 0
            and self._iters_since_skill >= self._skill_nudge_interval):
        self._spawn_background_review(
            messages_snapshot=list(self.messages),
            review_skills=True,
        )
        self._iters_since_skill = 0  # 重置计数器
    
    return response
```

注意：
- 审查在**主对话响应完毕后**触发，不影响用户体验
- 传入的是消息列表的**快照**（`list(self.messages)`），避免主对话继续修改消息历史时产生竞争
- 计数器在触发后重置为 0

### 为什么是工具调用次数而不是对话轮次？

因为工具调用次数更能反映任务的复杂度。用户说"帮我重构这个模块"，可能只有一轮对话但触发了 20+ 次工具调用。这种复杂任务更可能产生值得保存的经验。

## 后台审查 Agent

触发后，我们 fork 一个独立的 Agent 在后台线程中运行：

```python
import threading

# 审查提示词
_SKILL_REVIEW_PROMPT = (
    "Review the conversation above and consider saving or updating a skill "
    "if appropriate.\n\n"
    "Focus on: was a non-trivial approach used to complete a task that required "
    "trial and error, or changing course due to experiential findings along the "
    "way, or did the user expect or desire a different method or outcome?\n\n"
    "If a relevant skill already exists, update it with what you learned. "
    "Otherwise, create a new skill if the approach is reusable.\n"
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

_COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and consider two things:\n\n"
    "**Memory**: Has the user revealed things about themselves — their persona, "
    "desires, preferences, or personal details? If so, save using the memory tool.\n\n"
    "**Skills**: Was a non-trivial approach used to complete a task that required "
    "trial and error, or changing course? If a relevant skill already exists, "
    "update it. Otherwise, create a new one if the approach is reusable.\n\n"
    "Only act if there's something genuinely worth saving. "
    "If nothing stands out, just say 'Nothing to save.' and stop."
)
```

审查提示词的设计有几个关键点：

1. **"非平凡方法"**：排除简单的一步操作，只关注需要试错、调整方向的复杂任务
2. **"更新已有 Skill"**：如果相关 Skill 已经存在，优先更新而不是创建新的
3. **"Nothing to save"**：明确告诉 Agent 可以什么都不做，防止过度生成

### fork 审查 Agent

```python
def _spawn_background_review(self, messages_snapshot, review_skills=False, review_memory=False):
    """启动后台审查线程"""
    
    # 选择提示词
    if review_memory and review_skills:
        prompt = _COMBINED_REVIEW_PROMPT
    elif review_skills:
        prompt = _SKILL_REVIEW_PROMPT
    else:
        prompt = _MEMORY_REVIEW_PROMPT

    def _run_review():
        try:
            # 创建独立的 Agent 副本
            review_agent = Agent(
                config=self.config,
                max_iterations=8,       # 最多 8 次工具调用
                quiet_mode=True,        # 不产生用户可见输出
            )
            
            # 共享记忆存储——审查 Agent 的写入直接生效
            review_agent.memory_store = self.memory_store
            
            # 禁止审查 Agent 触发新的审查（防止递归）
            review_agent._skill_nudge_interval = 0
            review_agent._memory_nudge_interval = 0

            # 运行审查对话
            # 传入完整的消息历史 + 审查提示词作为新的用户消息
            review_agent.run_conversation(
                user_message=prompt,
                conversation_history=messages_snapshot,
            )

            # 扫描审查 Agent 的工具调用，提取结果摘要
            actions = _extract_review_actions(review_agent)
            if actions:
                summary = " · ".join(actions)
                print(f"  💾 {summary}")

        except Exception as e:
            pass  # 后台审查是尽力而为，失败不影响主流程

    # 在守护线程中运行
    t = threading.Thread(target=_run_review, daemon=True, name="bg-review")
    t.start()
```

几个关键设计：

- **`max_iterations=8`**：限制审查 Agent 的工具调用次数，防止它陷入复杂操作
- **`quiet_mode=True`**：不向用户输出任何内容
- **共享 `memory_store`**：审查 Agent 创建的 Skill 直接写入共享存储
- **`nudge_interval = 0`**：审查 Agent 自己不触发新的审查（否则会无限递归）
- **daemon 线程**：主进程退出时后台线程自动结束

### 提取审查结果

审查完成后，扫描工具调用记录，提取成功的操作：

```python
def _extract_review_actions(review_agent):
    """从审查 Agent 的消息历史中提取成功的操作"""
    actions = []
    
    for msg in review_agent.messages:
        if msg.get("role") != "tool":
            continue
        
        try:
            data = json.loads(msg.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        
        if not data.get("success"):
            continue
        
        message = data.get("message", "")
        if "created" in message.lower():
            actions.append(message)
        elif "updated" in message.lower():
            actions.append(message)
    
    return actions
```

用户看到的效果：

```
> 帮我把 agent/config.py 里的配置迁移到 YAML 格式

[Agent 执行了 15 次工具调用，完成任务]

assistant: 配置迁移完成。原来的 Python dict 格式已经转换为 YAML...

  💾 Skill 'python-to-yaml-migration' created
```

最后一行是后台审查的结果——Agent 自动把"Python 配置迁移到 YAML"的经验保存为了一个 Skill。

## skill_manage 工具

审查 Agent 需要一个工具来创建和管理 Skill。我们实现一个 `skill_manage` 工具：

```python
# agent/tools/skill_manage.py

class SkillManageTool:
    name = "skill_manage"
    description = "Create, edit, or delete skills"
    
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "edit", "delete"],
                "description": "The action to perform",
            },
            "name": {
                "type": "string",
                "description": "Skill name (lowercase, hyphens allowed)",
            },
            "category": {
                "type": "string",
                "description": "Category for the skill (e.g., 'development', 'devops')",
            },
            "content": {
                "type": "string",
                "description": "Full SKILL.md content (YAML frontmatter + markdown body)",
            },
        },
        "required": ["action", "name"],
    }

    def execute(self, action, name, category="general", content=""):
        if action == "create":
            return self._create_skill(name, category, content)
        elif action == "edit":
            return self._edit_skill(name, content)
        elif action == "delete":
            return self._delete_skill(name)
        else:
            return {"success": False, "message": f"Unknown action: {action}"}

    def _create_skill(self, name, category, content):
        """创建新 Skill"""
        # 验证名称
        if not re.match(r'^[a-z0-9][a-z0-9-]*$', name):
            return {"success": False, "message": "Invalid skill name"}
        
        if len(content) > 100000:
            return {"success": False, "message": "Content too large (max 100K chars)"}

        # 确定存储路径
        skills_dir = Path.home() / ".coding-agent" / "skills" / category / name
        skill_file = skills_dir / "SKILL.md"
        
        if skill_file.exists():
            return {"success": False, "message": f"Skill '{name}' already exists"}

        # 创建目录和文件
        skills_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(content, encoding="utf-8")

        return {"success": True, "message": f"Skill '{name}' created"}

    def _edit_skill(self, name, content):
        """编辑已有 Skill"""
        skill_file = self._find_skill(name)
        if skill_file is None:
            return {"success": False, "message": f"Skill '{name}' not found"}

        if len(content) > 100000:
            return {"success": False, "message": "Content too large"}

        # 原子写入：先写临时文件，再替换
        tmp = skill_file.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(skill_file)

        return {"success": True, "message": f"Skill '{name}' updated"}

    def _delete_skill(self, name):
        """删除 Skill"""
        skill_file = self._find_skill(name)
        if skill_file is None:
            return {"success": False, "message": f"Skill '{name}' not found"}

        skill_file.unlink()
        # 清理空目录
        parent = skill_file.parent
        if not any(parent.iterdir()):
            parent.rmdir()

        return {"success": True, "message": f"Skill '{name}' deleted"}

    def _find_skill(self, name):
        """在所有 Skill 目录中查找指定 Skill"""
        base = Path.home() / ".coding-agent" / "skills"
        for skill_file in base.rglob("SKILL.md"):
            if skill_file.parent.name == name:
                return skill_file
        return None
```

注意 `_edit_skill` 使用了**原子写入**——先写临时文件再 `replace`，避免写入中途断电导致文件损坏。

## 记忆 vs 技能：分工原则

Hermes 明确区分了两种持久化知识：

| | 记忆（Memory） | 技能（Skill） |
|---|---|---|
| 知识类型 | **陈述性**——事实和偏好 | **程序性**——步骤和方法 |
| 典型内容 | "用户偏好 TypeScript"<br>"项目用 pnpm 而不是 npm" | "Python 配置迁移到 YAML 的步骤"<br>"部署到 k8s 的完整流程" |
| 触发计数 | `_turns_since_memory`（按轮次） | `_iters_since_skill`（按工具调用次数） |
| 存储格式 | 结构化的 key-value | YAML frontmatter + Markdown |

两个系统**独立计数、独立触发**。可能在一次对话结束后，记忆审查和技能审查同时触发，这时候用组合提示词：

```python
# 检查是否触发
_should_review_memory = (
    self._memory_nudge_interval > 0
    and self._turns_since_memory >= self._memory_nudge_interval
)
_should_review_skills = (
    self._skill_nudge_interval > 0
    and self._iters_since_skill >= self._skill_nudge_interval
)

if _should_review_memory or _should_review_skills:
    self._spawn_background_review(
        messages_snapshot=list(self.messages),
        review_memory=_should_review_memory,
        review_skills=_should_review_skills,
    )
```

## 自动生成的 Skill 长什么样

假设用户让 Agent 帮忙配置 GitHub Actions 做 Python 项目的 CI，经过了多轮试错。后台审查 Agent 可能会生成这样的 Skill：

````markdown
---
name: python-github-actions-ci
description: Set up GitHub Actions CI for Python projects with pytest and coverage
---

## Setup Steps

1. Create `.github/workflows/ci.yml`
2. Configure Python version matrix (3.10, 3.11, 3.12)
3. Install dependencies with `pip install -e ".[dev]"`
4. Run pytest with coverage: `pytest --cov=src --cov-report=xml`
5. Upload coverage to Codecov (optional)

## Key Gotchas

- Use `actions/setup-python@v5` (not v4, it has caching issues with pip)
- Pin dependency versions in CI to avoid flaky builds
- Add `PYTHONPATH: .` to env if imports fail
- For monorepo: use `working-directory` to scope each job

## Template

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: pytest --cov=src
```
````

这个 Skill 包含了步骤、踩坑经验和模板——都是 Agent 在这次对话中"学到"的。下次用户再配置 CI 时，Agent 可以直接加载这个 Skill，跳过试错阶段。

## 与 Hermes Agent 的差异

Hermes 在此基础上有更多生产级考量：

1. **安全扫描**：新创建的 Skill 会经过 `skills_guard` 模块检查恶意代码和提示注入
2. **模糊匹配补丁**：`skill_manage` 的 `patch` 操作支持模糊匹配的局部替换，处理空格和缩进差异
3. **支持文件目录**：Skill 可以包含 `references/`、`templates/`、`scripts/` 子目录
4. **技能中心**：通过 `agentskills.io` 发现和安装社区共享的 Skill
5. **信任级别**：builtin / trusted / community 三级信任，不同级别的安全扫描严格度不同

我们的实现保留了核心机制——后台审查 Agent + 工具调用次数触发 + skill_manage 工具。这已经足够让 Agent 具备"从经验中学习"的基本能力。

## 小结

技能自生成的核心机制：

- **触发**：工具调用计数器达到阈值（默认 10 次）
- **审查**：fork 一个后台 Agent（`max_iterations=8`，`quiet_mode=True`）
- **决策**：审查 Agent 通过审查提示词回顾对话，自行判断是否创建/更新 Skill
- **执行**：通过 `skill_manage` 工具创建 Skill 文件
- **反馈**：扫描工具调用结果，给用户显示一行摘要

整个机制的精妙之处在于它不需要任何专门的"学习算法"——LLM 本身就有足够的判断力来决定什么值得记住。我们只需要给它一个回顾的机会。

下一篇我们来升级会话存储——从 JSONL 文件迁移到 SQLite + FTS5 全文搜索，让历史对话可以被检索和利用。
