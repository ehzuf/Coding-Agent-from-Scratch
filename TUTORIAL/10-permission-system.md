# 从零实现 Coding Agent（十）：权限系统

Bash 工具让 agent 能够执行任意命令，这是强大的能力，也是危险的源头。我们需要一个权限系统来控制它。

## 为什么需要权限系统？

想象这个场景：

```
用户: "清理临时文件"
Agent: [bash] rm -rf /tmp/*
       [bash] rm -rf ~/Downloads/*
       [bash] rm -rf /*  # 灾难！
```

Agent 可能误删重要文件。权限系统的作用是：
1. 在执行危险操作前确认
2. 阻止明显危险的操作
3. 让用户控制 agent 能做什么

## Ask 模式（Human-in-the-loop）

### 设计思想

Ask 模式是最基本的权限控制：在执行工具前询问用户。

```python
# agent/permission.py

class PermissionManager:
    def ask_user(self, tool_name: str, argument: str) -> bool:
        """询问用户是否允许执行。"""
        print(f"\n[权限请求] 工具: {tool_name}")
        print(f"  参数: {argument}")
        print("  允许执行? [y/n/a(yes to all)] ", end="", flush=True)

        response = input().strip().lower()
        return response in ("y", "yes", "a")
```

### 集成到工具执行

```python
# agent/agent.py

def _execute_tool(self, tool_use: dict) -> str:
    tool_name = tool_use.get("name", "")
    tool_input = tool_use.get("input", {})

    # 权限检查
    if self.enable_permission:
        argument = self._get_tool_argument(tool_use)
        allowed, reason = self.permission_manager.should_execute(tool_name, argument)
        if not allowed:
            return f"错误：权限拒绝 - {reason}"

    # 执行工具
    # ...
```

### 交互示例

```
[权限请求] 工具: bash
  参数: rm -rf /tmp/test
  允许执行? [y/n/a(yes to all)] y

[bash] stdout:
已删除 /tmp/test
```

用户输入：
- `y` 或 `yes`：允许这次操作
- `n` 或 `no`：拒绝这次操作
- `a`：允许所有后续操作（yes to all）

## Allow/Deny 规则

### 设计思想

每次都确认太繁琐。我们需要规则来自动判断：
- 某些操作自动允许（如 `git status`）
- 某些操作自动拒绝（如 `rm -rf /*`）
- 其他操作询问用户

### 规则格式

```
tool_name(pattern) -> allow/deny/ask
```

示例：
- `bash(git *)`: 允许执行 git 开头的命令
- `bash(rm -rf *)`: 拒绝强制删除
- `read(*.py)`: 允许读取 Python 文件
- `write(/tmp/*)`: 允许写入 /tmp 目录

### 实现

```python
@dataclass
class PermissionRule:
    tool_name: str       # 工具名称
    pattern: str         # 匹配模式
    mode: PermissionMode # 权限模式

    def matches(self, tool_name: str, argument: str) -> bool:
        if tool_name != self.tool_name:
            return False
        # 使用 fnmatch 进行通配符匹配
        return fnmatch.fnmatch(argument, self.pattern)
```

### Fail-Closed 设计

规则检查顺序很关键：

```python
def check_permission(self, tool_name: str, argument: str) -> PermissionDecision:
    # 1. 先检查 deny 规则（安全优先）
    for rule in self.deny_rules:
        if rule.matches(tool_name, argument):
            return PermissionDecision.DENY

    # 2. 再检查 allow 规则
    for rule in self.allow_rules:
        if rule.matches(tool_name, argument):
            return PermissionDecision.ALLOW

    # 3. 使用默认模式
    return self.default_mode
```

先检查 deny，再检查 allow。这确保危险操作不会被误判为允许。

### 预设配置

```python
def get_default_permission_config() -> PermissionConfig:
    """默认配置（安全模式）"""
    return PermissionConfig(
        default_mode=PermissionMode.ASK,
        allow_rules=[
            PermissionRule.parse("read(*)", PermissionMode.ALLOW),
            PermissionRule.parse("glob(*)", PermissionMode.ALLOW),
            PermissionRule.parse("grep(*)", PermissionMode.ALLOW),
        ],
        deny_rules=[
            PermissionRule.parse("bash(rm -rf /*)", PermissionMode.DENY),
            PermissionRule.parse("bash(dd *)", PermissionMode.DENY),
        ],
    )

def get_permissive_permission_config() -> PermissionConfig:
    """宽松配置（自动允许所有）"""
    return PermissionConfig(
        default_mode=PermissionMode.ALLOW,
        deny_rules=[
            PermissionRule.parse("bash(rm -rf /*)", PermissionMode.DENY),
        ],
    )

def get_strict_permission_config() -> PermissionConfig:
    """严格配置（自动拒绝所有未明确允许的）"""
    return PermissionConfig(
        default_mode=PermissionMode.DENY,
        allow_rules=[
            PermissionRule.parse("read(*)", PermissionMode.ALLOW),
        ],
    )
```

## 三种权限模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `ask` | 默认询问，只读操作自动允许 | 日常使用 |
| `allow` | 自动允许所有，只拒绝最危险的 | 自动化脚本 |
| `strict` | 只允许明确允许的操作 | 高安全环境 |

## 使用示例

### CLI 使用

```bash
# 默认模式
python -m agent "查看 git 状态"

# 宽松模式
python -m agent --permission-mode allow "执行任意命令"

# 严格模式
python -m agent --permission-mode strict "只读文件"
```

### Python 代码

```python
from agent.permission import (
    PermissionManager,
    PermissionConfig,
    PermissionRule,
    PermissionMode,
)

# 自定义配置
config = PermissionConfig(
    default_mode=PermissionMode.ASK,
    allow_rules=[
        PermissionRule.parse("bash(git *)", PermissionMode.ALLOW),
        PermissionRule.parse("bash(ls *)", PermissionMode.ALLOW),
    ],
    deny_rules=[
        PermissionRule.parse("bash(rm *)", PermissionMode.DENY),
        PermissionRule.parse("bash(sudo *)", PermissionMode.DENY),
    ],
)

manager = PermissionManager(config)

# 检查权限
allowed, reason = manager.should_execute("bash", "git status")
# -> (True, "allowed by rule")

allowed, reason = manager.should_execute("bash", "rm file.txt")
# -> (False, "denied by rule")
```

## 通配符匹配

使用 Python 的 `fnmatch` 模块：

| 模式 | 匹配 | 不匹配 |
|------|------|--------|
| `*` | 任何内容 | - |
| `*.py` | `test.py`, `main.py` | `test.txt` |
| `git *` | `git status`, `git commit` | `ls` |
| `/tmp/*` | `/tmp/file`, `/tmp/dir/file` | `/home/file` |

## 这一步我们学到了什么

1. **Human-in-the-loop 是基础安全**：让用户确认危险操作
2. **规则系统减少交互**：自动判断常见情况
3. **Fail-closed 设计**：安全优先，宁可拒绝不可误放
4. **权限模式适配场景**：ask 适合交互，strict 适合高安全

权限系统是 coding agent 安全使用的基石。没有它，agent 可能造成不可逆的损害。

下一篇将实现 Prompt Caching——利用缓存大幅降低重复输入的 token 开销。
