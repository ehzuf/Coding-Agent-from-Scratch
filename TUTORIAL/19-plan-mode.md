# 从零实现 Coding Agent（十九）：Plan Mode

前面十八篇，Agent 拿到任务就直接开始执行——读文件、改代码、跑命令。对于简单任务没问题，但复杂任务经常出现：**改到一半发现方向错了，或者遗漏了关键依赖关系。**

比如"重构 config 模块为多文件结构"，Agent 直接上手拆文件，结果忘了更新所有 import 路径，或者没注意到循环依赖。如果先花几分钟读代码、分析依赖、制定方案再动手，效果会好得多。

本篇实现 **Plan Mode（规划模式）**：让 Agent 先规划再执行。

这对应 Claude Code 源码中的 `tools/EnterPlanModeTool/` + `tools/ExitPlanModeTool/`。

---

## 核心思路

Plan Mode 的本质是 **限制 Agent 的工具权限**：

```
正常模式: 所有工具可用（read, write, edit, bash, ...）
规划模式: 只有只读工具可用（read, glob, grep, bash 只读命令）
```

这迫使 Agent 在规划阶段只能"看"不能"动"——必须充分理解代码后才能提出方案。方案提交后恢复全部权限，按方案执行。

整个流程：

```
用户: "重构 config.py 为多文件结构"
  ↓
Agent 调用 enter_plan_mode
  ↓ [规划模式：只读工具]
Agent 调用 read（读 config.py）
Agent 调用 grep（搜索所有 import config 的文件）
Agent 调用 bash（git log --oneline config.py 看改动历史）
  ↓
Agent 调用 exit_plan_mode（提交方案）
  ↓ [正常模式：全部工具恢复]
Agent 调用 write（创建 config/base.py）
Agent 调用 edit（更新 import 路径）
Agent 调用 bash（运行测试）
```

---

## 两个工具

### EnterPlanModeTool

```python
class EnterPlanModeTool(Tool):
    @property
    def name(self):
        return "enter_plan_mode"

    @property
    def description(self):
        return (
            "进入规划模式，用于复杂任务的前期分析和方案设计。"
            "进入后只能使用只读工具（read, glob, grep 等），不能修改文件。"
            "完成规划后调用 exit_plan_mode 提交方案并恢复写入权限。"
        )

    @property
    def input_schema(self):
        return {"type": "object", "properties": {}, "required": []}

    def call(self, input):
        return "已进入规划模式。只能使用只读工具，请分析代码、制定方案。"
```

无参数。返回值只是给 LLM 看的确认信息。实际的模式切换在 Agent 中处理。

### ExitPlanModeTool

```python
class ExitPlanModeTool(Tool):
    @property
    def name(self):
        return "exit_plan_mode"

    @property
    def input_schema(self):
        return {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "实施方案（markdown 格式），包含分析结论和具体步骤",
                },
            },
            "required": ["plan"],
        }

    def call(self, input):
        plan = input.get("plan", "")
        return f"已退出规划模式，恢复全部工具权限。\n\n## 方案\n\n{plan}\n\n现在可以开始执行。"
```

`plan` 参数是方案内容。这段内容会作为 tool_result 留在消息历史中，后续 Agent 执行时可以参考。

---

## 只读判断

```python
READONLY_TOOLS = {"read", "glob", "grep", "get_current_time", "agent", "send_message"}

BASH_READONLY_PREFIXES = (
    "ls", "cat", "head", "tail", "find", "grep", "rg",
    "wc", "file", "which", "echo", "pwd", "tree", "du", "df",
    "git status", "git log", "git diff", "git show", "git branch",
    "git remote", "git tag",
    "python --version", "python3 --version", "node --version",
    "pip list", "pip show",
)

def is_tool_readonly(tool_name, tool_input):
    if tool_name in READONLY_TOOLS:
        return True
    if tool_name == "bash":
        command = tool_input.get("command", "").strip()
        for prefix in BASH_READONLY_PREFIXES:
            if command == prefix or command.startswith(prefix + " "):
                return True
        return False
    if tool_name in ("enter_plan_mode", "exit_plan_mode"):
        return True
    return False
```

`bash` 工具的判断比较特殊——不能一刀切禁止，因为 `ls`、`git log` 这些只读命令在规划阶段很有用。用命令前缀白名单来判断。

这个设计是保守的：**不在白名单里的一律拒绝**。宁可让 Agent 退出规划模式后再执行，也不冒险在规划阶段执行可能有副作用的命令。

---

## Agent 集成

在 `_execute_tool()` 中，权限检查之后加入 Plan Mode 检查：

```python
def _execute_tool(self, tool_use):
    tool_name = tool_use.get("name", "")
    tool_input = tool_use.get("input", {})

    # 权限检查 ...

    # Plan Mode 检查
    if self.plan_mode and not is_tool_readonly(tool_name, tool_input):
        return (
            f"错误：规划模式下不允许执行 '{tool_name}'。"
            "请先调用 exit_plan_mode 提交方案后再执行修改操作。"
        )

    # PreToolUse Hook ...
    # 执行工具 ...

    # Plan Mode 状态切换（在工具成功执行后修改，避免 Hook 阻止后状态不一致）
    if tool_name == "enter_plan_mode":
        self.plan_mode = True
    elif tool_name == "exit_plan_mode":
        self.plan_mode = False
```

关键设计：
- **状态在工具执行后切换** —— 先执行工具，成功后再修改状态，避免 Hook 阻止时状态不一致
- **exit_plan_mode 执行后恢复** —— 设 `plan_mode = False`，后续工具调用恢复正常
- **Plan Mode 检查在权限检查之后** —— 权限系统是硬约束，Plan Mode 是软约束。被权限拒绝的操作不需要再检查 Plan Mode

工具注册在 `__init__` 中，与 AgentTool、SendMessageTool 一起：

```python
if _enable_agent_tool:
    # ... agent_tool, send_message_tool ...
    self.tools.append(EnterPlanModeTool())
    self.tools.append(ExitPlanModeTool())
```

注意只有主 Agent 添加 Plan Mode 工具。子 Agent（`_enable_agent_tool=False`）不需要规划模式。

---

## 与 Claude Code 的对比

| 维度 | Claude Code | 我们的实现 |
|------|-------------|------------|
| 方案存储 | 写入磁盘文件（plan file） | 保留在消息历史中 |
| 用户审批 | 退出时弹出审批对话框 | 无审批（信任 Agent 方案） |
| 权限请求 | 方案中声明需要的权限 | 不支持（简化） |
| 面试阶段 | 可选的 interview phase | 不支持（简化） |
| 只读判断 | 工具自带 isReadOnly() 方法 | 全局白名单 + bash 前缀匹配 |
| 任务跟踪 | TodoWriteTool 管理任务清单 | 不支持（简化） |

我们的简化：
- **不写文件** —— 方案直接在 tool_result 中，作为消息历史的一部分。好处是方案天然参与后续对话上下文
- **不需要审批** —— Agent 自己决定何时规划、何时执行。实际使用中 Agent 的方案质量通常足够好
- **白名单而非接口** —— Claude Code 每个工具有 `isReadOnly()` 方法，我们用全局白名单简化，不需要改动每个工具类
- **无 TodoWriteTool** —— Claude Code 还有 `TodoWriteTool` 将方案分解为可跟踪的任务清单。本教程仅实现核心的"权限控制"概念，任务跟踪依赖 Agent 自行管理

---

## 使用效果

Agent 会在遇到复杂任务时主动进入规划模式：

```
你: 帮我把 agent/config.py 重构为 config 包

Agent: 这是一个涉及多文件的重构任务，让我先进入规划模式分析。
  → [enter_plan_mode]
  → [read] agent/config.py（分析现有结构）
  → [grep] "from agent.config" 搜索所有引用
  → [grep] "import.*config" 搜索间接引用
  → [bash] git log --oneline agent/config.py（查看最近改动）
  → [exit_plan_mode] 提交方案:
    1. 创建 agent/config/ 目录
    2. 拆分为 base.py（Config 类）、env.py（环境变量）、cli.py（CLI 参数）
    3. __init__.py 重新导出所有公开 API
    4. 更新 12 个文件的 import 路径
    5. 运行测试验证

Agent: 方案确认，开始执行。
  → [bash] mkdir agent/config
  → [write] agent/config/base.py
  → ...
```

如果 Agent 在规划模式下不小心尝试写入：

```
  → [write] agent/config/base.py
  → 错误：规划模式下不允许执行 'write'。请先调用 exit_plan_mode 提交方案。
```

Agent 会收到这个错误，然后调用 exit_plan_mode 提交方案。

---

## 小结

这一篇实现了 Plan Mode：

1. **两个工具** —— EnterPlanModeTool / ExitPlanModeTool，Agent 自主决定何时规划
2. **只读限制** —— 规划模式下只允许 read、glob、grep 和 bash 只读命令
3. **方案即上下文** —— exit_plan_mode 的 plan 参数留在消息历史中，指导后续执行
4. **零配置** —— 不需要任何配置，工具自动注册到主 Agent

Plan Mode 解决的核心问题：**强制 Agent "三思而后行"**。对于简单任务，Agent 不会调用 enter_plan_mode，直接执行。对于复杂任务，System Prompt 可以引导 Agent 先规划再动手。

下一篇将实现 Session Memory——在长对话中定期生成结构化笔记，防止上下文压缩时丢失关键信息。
