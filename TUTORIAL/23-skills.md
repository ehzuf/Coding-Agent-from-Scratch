# 从零实现 Coding Agent（23）：Skills 系统

前面我们实现了 MCP Client，让 Agent 能动态连接外部工具。但 Agent 的行为模式仍然是通用的——面对不同类型的任务，它使用相同的系统提示和工具集。

本篇实现 **Skills 系统**——让用户通过简单的 Markdown 文件定义可复用的工作流模板，Agent 可以按需调用这些 Skill，fork 出专用子 Agent 执行特定任务。

## 什么是 Skill

Skill（技能）是一个 **Markdown 文件 + Frontmatter 元数据** 的组合：

```markdown
---
name: code-review
description: 审查代码变更并给出建议
allowed_tools: [read, glob, grep]
context: fork
---

请审查以下代码变更，关注：
1. 正确性——逻辑错误、边界条件
2. 安全性——注入、权限问题
3. 性能——不必要的循环、内存分配
4. 可读性——命名、注释、代码结构
```

**Frontmatter** 是文件头部 `---` 包围的元数据区域，声明 Skill 的名称、描述和约束条件。
**正文**（body）是 Skill 的 prompt 内容，会作为子 Agent 的系统指令。

这种格式让 Skill 定义既直观（就是一个文档），又结构化（机器可解析 frontmatter）。

## Skill 来源

Skill 文件按两级目录加载：

```
~/.coding-agent/skills/          ← 用户级（全局共享）
  code-review.md
  commit-message.md
  
.coding-agent/skills/            ← 项目级（仅当前项目）
  deploy-check.md
  test-strategy.md
```

加载规则：
1. 先加载用户级 `~/.coding-agent/skills/*.md`
2. 再加载项目级 `.coding-agent/skills/*.md`
3. **同名覆盖**：项目级的 Skill 覆盖用户级同名 Skill

这样可以在项目中定制特定的工作流，同时共享全局通用 Skill。

## Frontmatter 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 否 | Skill 名称（默认取文件名） |
| `description` | string | 否 | 描述（展示给 LLM 和用户） |
| `allowed_tools` | list | 否 | 子 Agent 可用工具白名单 |
| `context` | string | 否 | `fork`（默认）或 `inline` |

### allowed_tools——能力边界控制

`allowed_tools` 是 Skills 系统的核心安全机制。它限制子 Agent 只能使用指定的工具：

```markdown
---
name: code-review
allowed_tools: [read, glob, grep]
---
```

这个 Skill 只能读取代码，不能修改文件或执行命令。如果不指定 `allowed_tools`，子 Agent 会继承父 Agent 的大部分工具（排除 `skill`、`agent`、`send_message` 等递归风险工具）。

## Frontmatter 解析

Skills 使用简化的 frontmatter 解析器，不依赖 PyYAML：

```python
def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """
    解析 markdown frontmatter，返回 (frontmatter_dict, body)。
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
```

解析规则：
1. 用正则匹配 `---` 包围的区域
2. 逐行按 `key: value` 分割
3. 值以 `[` 开头时解析为列表

这个解析器足够处理 Skills 需要的简单 YAML 子集，不需要引入 PyYAML 依赖。

## Skill 加载

```python
def load_skills(cwd: str = ".") -> list[SkillDefinition]:
    """从所有来源加载 Skill 定义。"""
    skills: dict[str, SkillDefinition] = {}

    # 用户级
    user_dir = os.path.join(AGENT_HOME, SKILLS_DIRNAME)
    _load_skills_from_dir(user_dir, "user", skills)

    # 项目级（覆盖同名）
    project_dir = os.path.join(cwd, ".coding-agent", SKILLS_DIRNAME)
    _load_skills_from_dir(project_dir, "project", skills)

    return list(skills.values())
```

`_load_skills_from_dir` 遍历目录中的 `.md` 文件，解析 frontmatter 和 body，构建 `SkillDefinition`：

```python
@dataclass
class SkillDefinition:
    """一个 Skill 的完整定义。"""
    name: str
    description: str
    prompt: str          # frontmatter 之后的 markdown 内容
    source: str          # 来源路径
    allowed_tools: list[str] = field(default_factory=list)
    context: str = "fork"  # "inline" 或 "fork"
```

使用 `dict[str, SkillDefinition]` 存储结果，key 是 Skill 名称。项目级加载在用户级之后，同名自动覆盖。

## SkillTool——Agent 执行 Skill

SkillTool 是一个 Tool 子类，让 Agent 通过标准的 Tool Use 协议调用 Skill：

```python
class SkillTool(Tool):
    @property
    def name(self) -> str:
        return "skill"

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
```

LLM 看到 `skill` 工具后，可以选择调用特定的 Skill。输入参数包含 Skill 名称和可选参数。

### 执行流程：fork 子 Agent

```python
def call(self, tool_input: dict) -> str:
    skill_name = tool_input.get("name", "")
    args = tool_input.get("args", "")
    skill = self._skills.get(skill_name)

    if not skill:
        return f"错误：未知 Skill '{skill_name}'。可用: {list(self._skills.keys())}"

    # 构建 Skill prompt
    prompt = skill.prompt
    if args:
        prompt = f"{prompt}\n\n用户参数: {args}"

    # 选择可用工具
    if skill.allowed_tools:
        sub_tools = [t for t in self._parent_tools if t.name in skill.allowed_tools]
    else:
        sub_tools = [
            t for t in self._parent_tools
            if t.name not in ("skill", "agent", "send_message",
                              "enter_plan_mode", "exit_plan_mode")
        ]

    # Fork 子 Agent 执行
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
```

关键设计：
1. **fork 子 Agent**：独立消息历史，不污染主对话
2. **工具过滤**：根据 `allowed_tools` 约束子 Agent 的能力
3. **排除递归工具**：`skill`、`agent`、`send_message` 等不传递给子 Agent
4. **关闭压缩**：Skill 任务通常短小，不需要自动压缩
5. **关闭权限**：由父 Agent 的权限系统统一管控
6. **关闭 agent_tool**：`_enable_agent_tool=False` 防止子 Agent 再启动子 Agent

## SkillManager

SkillManager 是 Skill 的管理入口：

```python
class SkillManager:
    def __init__(self, cwd: str = "."):
        self.cwd = cwd
        self.skills = load_skills(cwd)

    def has_skills(self) -> bool:
        return len(self.skills) > 0

    def create_skill_tool(self, llm, parent_tools, max_turns=10) -> SkillTool:
        return SkillTool(
            skills=self.skills, llm=llm,
            parent_tools=parent_tools, max_turns=max_turns,
        )

    def get_summary(self) -> str:
        """获取 Skill 摘要信息。"""
        ...

    def list_skill_names(self) -> list[str]:
        return [s.name for s in self.skills]
```

## 集成到 Agent

在 `build_agent()` 中加载 Skills：

```python
# Skills 系统：加载自定义 Skill
skill_manager = SkillManager(cwd=config.cwd)
if skill_manager.has_skills():
    skill_tool = skill_manager.create_skill_tool(
        llm=llm, parent_tools=tools, max_turns=min(config.max_turns, 10),
    )
    tools.append(skill_tool)

# 创建 Agent ...
agent = Agent(llm=llm, tools=tools, ...)

# 存储引用
agent._skill_manager = skill_manager
```

启动时显示加载状态：

```python
if skill_manager.has_skills():
    print(f"  Skills: {skill_manager.list_skill_names()}")
```

REPL 新增 `/skills` 命令查看详情：

```python
if prompt == "/skills":
    skill_mgr = getattr(agent, "_skill_manager", None)
    if skill_mgr and skill_mgr.has_skills():
        print(f"\n{skill_mgr.get_summary()}\n")
    else:
        print("\n暂无可用 Skill。\n")
    continue
```

## 使用示例

### 创建 Skill

在 `~/.coding-agent/skills/` 下创建 `code-review.md`：

```markdown
---
name: code-review
description: 审查代码变更并给出建议
allowed_tools: [read, glob, grep, bash]
context: fork
---

请审查最近的代码变更（git diff），关注：
1. 正确性——逻辑错误、边界条件、空值处理
2. 安全性——注入攻击、权限泄露
3. 性能——不必要的循环、内存分配、N+1 查询
4. 可读性——命名、注释、函数长度

输出格式：
- 每个问题：文件名 + 行号 + 严重程度 + 描述 + 建议修复
- 最后给出总结评分（1-10）
```

### 运行 Agent

```bash
python -m agent
[anthropic / claude-sonnet-4-20250514] 工作目录: /Users/xxx/project
  Skills: ['code-review']

[1] 你: 请帮我审查最近的代码变更
```

LLM 会自动调用 `skill` 工具执行 `code-review` Skill，fork 一个只能读取代码的子 Agent 来审查。

### 查看 Skills

```bash
[2] 你: /skills
可用 Skill:
  code-review — 审查代码变更并给出建议 (工具: ['read', 'glob', 'grep', 'bash'])
    来源: [user] /Users/xxx/.coding-agent/skills/code-review.md
```

## Skill vs Agent Tool vs MCP Tool

三种扩展机制的定位不同：

| 维度 | Skill | Agent Tool | MCP Tool |
|---|---|---|---|
| **定义方式** | Markdown 文件 | Python 代码 | MCP Server |
| **执行方式** | Fork 子 Agent | 子 Agent 实例 | 远程调用 |
| **核心价值** | 复用 prompt + 工具约束 | 并行子任务 | 外部服务集成 |
| **适合场景** | 固定工作流（审查、测试、部署） | 动态子任务分解 | 数据库、API、搜索 |
| **技术门槛** | 写 Markdown | 无需代码 | 运行 MCP Server |

Skill 的独特优势：
1. **零代码**——写 Markdown 就是创建工具
2. **可约束**——`allowed_tools` 限制子 Agent 能力
3. **可复用**——跨项目共享通用工作流
4. **可覆盖**——项目级定制覆盖全局默认

## 设计回顾

Skills 系统的核心思想是 **Prompt as Tool**：

```
用户定义 Markdown → 解析 frontmatter → 注册为 SkillTool
                                                ↓
LLM 选择调用 → fork 子 Agent → 工具过滤 → 执行 Skill prompt
                                                ↓
                                         返回结果给主 Agent
```

这个设计让用户无需写 Python 代码就能创建可复用的 Agent 工作流。Frontmatter 提供了元数据声明能力，`allowed_tools` 提供了安全约束，fork 执行提供了上下文隔离。

至此，Step 18-23 全部完成。第二阶段的 6 个功能覆盖了：
- **项目感知**：分层记忆文件（Step 18）+ 跨会话记忆（Step 21）
- **智能记忆**：会话记忆（Step 20）+ 自动记忆提取（Step 21）
- **可扩展性**：MCP 外部工具（Step 22）+ 自定义 Skills（Step 23）+ 规划模式（Step 19）

Agent 已经具备了一个完整 Coding Agent 的核心能力。
