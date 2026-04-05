# 从零实现 Coding Agent（十七）：Hooks 系统

在前面十六篇中，我们的 Agent 已经能持久化会话、恢复中断的对话。但有一个问题：**Agent 的行为是封闭的，用户无法在工具执行前后注入自定义逻辑。**

比如：
- 想在 Agent 执行 `bash` 命令前检查命令是否安全？—— 做不到
- 想在 Agent 写文件后自动运行 linter？—— 做不到
- 想在每次对话前自动注入项目上下文？—— 也做不到

本篇实现 **Hooks 系统**：工具调用前后、用户消息提交时，执行用户自定义的 shell 脚本。

这对应 Claude Code 源码中的 `utils/hooks.ts` + `types/hooks.ts` + `schemas/hooks.ts`。

---

## 为什么需要 Hooks？

Hooks 的核心价值是 **可扩展性**。Agent 本身只负责 LLM 调用和工具执行，但用户的需求千变万化：

| 场景 | 没有 Hooks | 有了 Hooks |
|------|-----------|-----------|
| 禁止 Agent 删除特定文件 | 修改 Agent 源码 | 配置一个 PreToolUse Hook 检查文件路径 |
| 每次写文件后自动格式化 | 修改 Agent 源码 | 配置一个 PostToolUse Hook 运行 formatter |
| 对话开始时注入项目规范 | 每次手动粘贴 | UserPromptSubmit Hook 自动注入 |
| 审计所有工具调用 | 看日志 | PostToolUse Hook 写审计日志 |
| 禁止执行危险的 shell 命令 | 修改权限系统 | PreToolUse Hook 检查命令内容 |

关键：**用户不需要改 Agent 代码，只需要写配置文件和 shell 脚本。**

---

## 三种 Hook 事件

我们参考 Claude Code 的设计，实现三种 Hook 事件：

```
用户输入 → [UserPromptSubmit] → 发送给 LLM → LLM 回复
                                                  ↓
                                           需要调用工具
                                                  ↓
                                         [PreToolUse] → 阻止？→ 返回错误
                                                  ↓（通过）
                                            执行工具
                                                  ↓
                                        [PostToolUse] → 注入上下文
                                                  ↓
                                         继续对话循环
```

### PreToolUse

**触发时机**：工具执行前（权限检查之后）

**能做什么**：
- 阻止工具执行（exit code 2）
- 修改工具输入参数（JSON 输出 `updatedInput`）
- 注入额外上下文

**典型场景**：
```bash
#!/bin/bash
# 禁止删除 src/ 下的文件
if [ "$TOOL_NAME" = "bash" ]; then
    INPUT="$TOOL_INPUT"
    if echo "$INPUT" | grep -q "rm.*src/"; then
        echo "禁止删除 src/ 目录下的文件"
        exit 2  # exit 2 = 阻止
    fi
fi
exit 0
```

### PostToolUse

**触发时机**：工具执行后

**能做什么**：
- 在工具结果后追加上下文信息
- 记录审计日志
- 触发后续操作（如自动格式化）

**典型场景**：
```bash
#!/bin/bash
# 写文件后自动运行 linter
if [ "$TOOL_NAME" = "write" ] || [ "$TOOL_NAME" = "edit" ]; then
    FILE=$(echo "$TOOL_INPUT" | python -c "import sys,json; print(json.load(sys.stdin).get('file_path',''))")
    if [[ "$FILE" == *.py ]]; then
        ruff check "$FILE" --fix 2>&1
        echo "已自动运行 ruff check"
    fi
fi
```

### UserPromptSubmit

**触发时机**：用户提交消息后、发送给 LLM 前

**能做什么**：
- 在用户消息后追加项目上下文
- 验证用户输入
- 记录对话日志

**典型场景**：
```bash
#!/bin/bash
# 自动注入项目规范
if [ -f ".project-rules" ]; then
    echo "项目规范: $(cat .project-rules)"
fi
```

---

## 配置格式

Hooks 通过 JSON 配置文件定义，支持两级配置：

1. **全局配置**：`~/.coding-agent/settings.json` —— 所有项目生效
2. **项目配置**：`项目目录/.coding-agent/settings.json` —— 仅当前项目生效

项目配置的 Hook 追加到全局配置之后，两级同时生效。

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/check-bash-safety.sh",
            "timeout": 5
          }
        ]
      },
      {
        "matcher": "write",
        "hooks": [
          {
            "type": "command",
            "command": "echo 'About to write a file'"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "echo \"$TOOL_NAME executed\" >> /tmp/agent-audit.log"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "cat .project-context 2>/dev/null || true"
          }
        ]
      }
    ]
  }
}
```

配置结构：

- **hooks** —— 顶层字段，包含三种事件
- **matcher** —— 匹配模式，对于 PreToolUse/PostToolUse 匹配工具名，支持 `fnmatch` 通配符
  - `"bash"` → 只匹配 bash 工具
  - `"*"` 或 `""` → 匹配所有工具
  - `"file_*"` → 匹配 file_read、file_write 等
- **hooks** —— 命令列表，按顺序执行
  - `type` → 目前只支持 `"command"`（shell 命令）
  - `command` → shell 命令字符串
  - `timeout` → 超时时间（秒），默认 10

---

## 通信协议：环境变量 + Exit Code + JSON

Hook 和 Agent 之间的通信不用 IPC、不用 socket，就用最简单的机制：

### 输入：环境变量

Hook 通过环境变量接收上下文：

| 环境变量 | 说明 | 哪些事件 |
|---------|------|---------|
| `HOOK_EVENT` | 事件名（PreToolUse/PostToolUse/UserPromptSubmit） | 全部 |
| `TOOL_NAME` | 工具名 | PreToolUse, PostToolUse |
| `TOOL_INPUT` | 工具输入（JSON 字符串） | PreToolUse, PostToolUse |
| `TOOL_RESULT` | 工具执行结果（截断到 5000 字符） | PostToolUse |
| `USER_PROMPT` | 用户输入的消息 | UserPromptSubmit |
| `CWD` | 当前工作目录 | 全部 |

为什么选环境变量：
1. **零依赖** —— 任何语言、任何脚本都能读环境变量
2. **无需协议协商** —— 不用考虑序列化格式
3. **进程隔离** —— 子进程有自己的环境变量空间，不会污染父进程

### 输出：Exit Code

| Exit Code | 含义 | 行为 |
|-----------|------|------|
| 0 | 成功 | 继续执行 |
| 2 | 阻止 | PreToolUse 时阻止工具执行，stdout 作为阻止原因 |
| 其他 | 错误 | 记录错误，不阻止执行（非阻塞错误） |

为什么 exit 2 = 阻止？这是 Claude Code 的约定。exit 1 通常表示"一般错误"，很多脚本不小心 exit 1 了不应该阻止工具执行。exit 2 是明确的"用户有意阻止"。

### 结构化输出：JSON 协议

Hook 的 stdout 如果是 JSON 格式，支持两个特殊字段：

```json
{
  "updatedInput": {"command": "ls -la", "timeout": 30},
  "additionalContext": "注意：此命令需要 sudo 权限"
}
```

- **updatedInput** —— 替换工具的输入参数（仅 PreToolUse 有效）
- **additionalContext** —— 追加到对话的上下文信息

非 JSON 输出直接作为 `additionalContext` 处理。

---

## 实现

### 数据结构

`agent/hooks.py` 定义了四个核心数据类：

```python
@dataclass
class HookCommand:
    """单个 Hook 命令。"""
    type: str = "command"
    command: str = ""
    timeout: int = 10  # 默认 10 秒

@dataclass
class HookMatcher:
    """匹配器：工具名匹配后执行 hooks 列表。"""
    matcher: str = ""       # 空字符串匹配所有
    hooks: list[HookCommand] = field(default_factory=list)

    def matches(self, value: str) -> bool:
        if not self.matcher:
            return True
        return fnmatch.fnmatch(value.lower(), self.matcher.lower())
```

**HookResult** 和 **AggregatedHookResult** 封装执行结果：

```python
@dataclass
class HookResult:
    outcome: str = "success"  # success, blocked, error, timeout
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    block_reason: str = ""
    additional_context: str = ""
    updated_input: dict | None = None

@dataclass
class AggregatedHookResult:
    """多个 Hook 的聚合结果。"""
    is_blocked: bool = False
    block_reason: str = ""
    additional_contexts: list[str] = field(default_factory=list)
    updated_input: dict | None = None
    results: list[HookResult] = field(default_factory=list)
```

为什么需要 AggregatedHookResult？因为一个事件可能匹配多个 Hook（比如一个全局 Hook + 一个项目 Hook）。聚合规则：
- 任何一个 Hook 阻止 → 整体阻止，停止后续 Hook
- additional_context 累积
- updated_input 取最后一个非 None 值

### 命令执行

`_run_hook_command()` 是最底层的执行函数：

```python
def _run_hook_command(hook, env, cwd):
    full_env = {**os.environ, **env}  # 合并系统环境变量

    try:
        result = subprocess.run(
            hook.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=hook.timeout,
            cwd=cwd,
            env=full_env,
        )
    except subprocess.TimeoutExpired:
        return HookResult(outcome="timeout", ...)
    except Exception as e:
        return HookResult(outcome="error", ...)

    # exit 2 = block
    if result.returncode == 2:
        return HookResult(outcome="blocked", block_reason=stdout or stderr)

    # 尝试解析 JSON 输出
    if stdout.startswith("{"):
        try:
            parsed = json.loads(stdout)
            updated_input = parsed.get("updatedInput")
            additional_context = parsed.get("additionalContext", "")
        except json.JSONDecodeError:
            additional_context = stdout
    else:
        additional_context = stdout

    return HookResult(outcome="success", additional_context=additional_context, ...)
```

关键设计：
- `shell=True` —— Hook 命令是 shell 字符串，支持管道、重定向等
- `{**os.environ, **env}` —— 继承系统环境变量，Hook 可以访问 PATH 等
- stdout 先尝试 JSON 解析，失败则整体作为 additionalContext
- 超时用 `subprocess.run(timeout=...)` 处理，不会挂起 Agent

### HookManager

`HookManager` 是外部接口：

```python
class HookManager:
    def __init__(self, cwd="."):
        self.cwd = os.path.abspath(cwd)
        self._hooks_config = {}
        self._load_config()

    def _load_config(self):
        global_hooks = self._load_hooks_from_file(~/.coding-agent/settings.json)
        project_hooks = self._load_hooks_from_file(项目/.coding-agent/settings.json)
        # 合并：项目配置追加到全局配置后面
        for event in HOOK_EVENTS:
            self._hooks_config[event] = global_hooks[event] + project_hooks[event]
```

三个公开方法对应三种事件：

```python
def run_pre_tool_use(self, tool_name, tool_input) -> AggregatedHookResult:
    hooks = self._find_matching_hooks("PreToolUse", tool_name)
    env = {"HOOK_EVENT": "PreToolUse", "TOOL_NAME": tool_name, "TOOL_INPUT": json.dumps(tool_input), "CWD": self.cwd}
    return self._run_hooks(hooks, env)

def run_post_tool_use(self, tool_name, tool_input, tool_result) -> AggregatedHookResult:
    hooks = self._find_matching_hooks("PostToolUse", tool_name)
    env = {"HOOK_EVENT": "PostToolUse", ..., "TOOL_RESULT": tool_result[:5000]}  # 截断
    return self._run_hooks(hooks, env)

def run_user_prompt_submit(self, prompt) -> AggregatedHookResult:
    hooks = self._find_matching_hooks("UserPromptSubmit", "")
    env = {"HOOK_EVENT": "UserPromptSubmit", "USER_PROMPT": prompt, "CWD": self.cwd}
    return self._run_hooks(hooks, env)
```

注意 `TOOL_RESULT` 截断到 5000 字符——环境变量有长度限制，工具结果可能非常大（比如读取一个长文件），不截断可能导致 `E2BIG` 错误。

### Agent 集成

Agent 只需要在三个点调用 HookManager：

**1. 用户消息提交时**

```python
def _run_hook_user_prompt(self, prompt: str) -> str:
    if not self.hook_manager:
        return prompt
    result = self.hook_manager.run_user_prompt_submit(prompt)
    if result.additional_contexts:
        context = "\n".join(result.additional_contexts)
        return prompt + f"\n\n[Hook context] {context}"
    return prompt
```

在 `_run_tool_loop()` 和 `stream()` 两个入口都调用：

```python
def _run_tool_loop(self, prompt):
    prompt = self._run_hook_user_prompt(prompt)  # Hook 注入
    self.messages.append({"role": "user", "content": prompt})
    ...
```

**2. 工具执行前后**

在 `_execute_tool()` 方法中，权限检查之后、工具执行之前：

```python
def _execute_tool(self, tool_use):
    # 权限检查 ...

    # PreToolUse Hook
    if self.hook_manager:
        pre_result = self.hook_manager.run_pre_tool_use(tool_name, tool_input)
        if pre_result.is_blocked:
            return f"错误：被 Hook 阻止 - {pre_result.block_reason}"
        if pre_result.updated_input is not None:
            tool_input = pre_result.updated_input

    # 执行工具 ...
    result = safe_tool_call(tool.call, tool_input)

    # PostToolUse Hook
    if self.hook_manager:
        post_result = self.hook_manager.run_post_tool_use(tool_name, tool_input, result)
        if post_result.additional_contexts:
            context = "\n".join(post_result.additional_contexts)
            result = result + f"\n\n[Hook context] {context}"

    return result
```

集成点选择的逻辑：
- **PreToolUse 在权限检查之后**：权限系统是硬性约束，Hook 是用户自定义逻辑。如果权限已经拒绝了，没必要再跑 Hook
- **PostToolUse 在 budget 截断之后**：Hook 看到的是最终返回给 LLM 的结果
- **UserPromptSubmit 在消息追加之前**：Hook 追加的 context 会成为用户消息的一部分

---

## 与 Claude Code 的对比

| 维度 | Claude Code | 我们的实现 |
|------|-------------|------------|
| Hook 类型 | Shell + HTTP + Function | Shell 命令 |
| 事件数量 | PreToolUse, PostToolUse, Notification, Stop, SubagentStop 等 | PreToolUse, PostToolUse, UserPromptSubmit |
| 匹配方式 | toolName 字段 + 正则 | fnmatch 通配符 |
| 配置来源 | settings.json + .claude/settings.json | ~/.coding-agent/settings.json + .coding-agent/settings.json |
| 进度展示 | Spinner + 状态 UI | 静默执行 |
| 并行执行 | 支持（parallel flag） | 顺序执行 |
| 输入修改 | JSON updatedInput | JSON updatedInput |

我们的简化：
- **只支持 Shell 命令**（vs HTTP Hook 和 Function Hook）—— MVP 足够，shell 能做任何事
- **三种事件**（vs Claude Code 的 5+ 种）—— 覆盖最核心的场景
- **fnmatch 通配符**（vs 正则）—— 配置更简单，`bash`、`file_*` 这种够用了
- **顺序执行**（vs 可选并行）—— 简单可靠，Hook 通常很快

Claude Code 更复杂的原因：
- 需要支持 VS Code 扩展场景（Function Hook）
- 需要支持远程 webhook（HTTP Hook）
- 需要更细粒度的事件（Notification、Stop 等用于 UI 控制）
- 需要并行执行优化（多个 Hook 同时运行）

---

## 使用示例

### 禁止执行危险命令

**~/.coding-agent/settings.json**：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash /path/to/check-command.sh"
          }
        ]
      }
    ]
  }
}
```

**check-command.sh**：

```bash
#!/bin/bash
# 检查 bash 工具的命令是否安全
COMMAND=$(echo "$TOOL_INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('command',''))")

# 禁止 rm -rf
if echo "$COMMAND" | grep -qE "rm\s+-rf\s+/"; then
    echo "禁止执行 rm -rf / 相关命令"
    exit 2
fi

# 禁止修改系统文件
if echo "$COMMAND" | grep -qE "(chmod|chown)\s+.*/(etc|usr|bin)"; then
    echo "禁止修改系统目录"
    exit 2
fi

exit 0
```

效果：

```
你: 帮我清理一下根目录的临时文件
Agent: 好的，让我执行 rm -rf /tmp/...
  → [bash] rm -rf /tmp/old-files
  → 错误：被 Hook 阻止 - 禁止执行 rm -rf / 相关命令
Agent: 看起来这个命令被安全策略阻止了...
```

### 自动格式化代码

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "write",
        "hooks": [
          {
            "type": "command",
            "command": "bash -c 'FILE=$(echo $TOOL_INPUT | python3 -c \"import sys,json; print(json.load(sys.stdin).get(\\\"file_path\\\",\\\"\\\"))\"); [[ \"$FILE\" == *.py ]] && ruff format \"$FILE\" 2>&1 && echo \"已自动格式化\" || true'"
          }
        ]
      }
    ]
  }
}
```

### 注入项目上下文

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "cat .coding-agent/project-context.md 2>/dev/null || true"
          }
        ]
      }
    ]
  }
}
```

每次用户输入消息时，自动把项目规范注入到 prompt 末尾。LLM 看到的消息变成：

```
用户的原始问题

[Hook context] # 项目规范
- 使用 Python 3.12+
- 代码风格遵循 ruff 规则
- 所有公开 API 需要 docstring
```

### 审计日志

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "echo \"$(date '+%Y-%m-%d %H:%M:%S') $TOOL_NAME\" >> ~/.coding-agent/audit.log"
          }
        ]
      }
    ]
  }
}
```

### 查看 Hook 配置

交互模式下用 `/hooks` 命令查看当前配置：

```
[1] 你: /hooks

Hooks 配置:
  PreToolUse (1 个):
    bash: ['/path/to/check-command.sh']
  PostToolUse (1 个):
    *: ['echo "$(date) $TOOL_NAME" >> ~/.coding-agent/audit.log']
  UserPromptSubmit (1 个):
    *: ['cat .coding-agent/project-context.md 2>/dev/null || true']
```

---

## CLI 参数

```bash
# 禁用 Hooks
python -m agent --no-hooks "帮我分析代码"

# 或通过环境变量
AGENT_NO_HOOKS=1 python -m agent "帮我分析代码"
```

`--no-hooks` 在调试时很有用——Hook 脚本有 bug 时可以跳过它们。

---

## 小结

这一篇实现了 Hooks 系统：

1. **三种事件** —— PreToolUse、PostToolUse、UserPromptSubmit，覆盖工具调用全生命周期
2. **Shell 命令执行** —— 通过环境变量传递上下文，exit code 控制行为
3. **配置驱动** —— JSON 配置文件 + fnmatch 通配符匹配，无需改代码
4. **双层配置** —— 全局 + 项目级，灵活管理不同项目的 Hook
5. **Agent 无侵入集成** —— 三个调用点，`if self.hook_manager` 守卫，不影响无 Hook 场景

Hooks 的设计哲学：**Agent 提供扩展点，用户通过配置和脚本实现个性化需求。** 这和 Git Hooks、Webpack Plugins、React 生命周期是同一个思路——框架负责"何时调用"，用户负责"调用什么"。

至此，我们的 Agent 已经具备了完整的生命周期钩子能力，用户可以在不修改源码的前提下深度定制 Agent 的行为。
