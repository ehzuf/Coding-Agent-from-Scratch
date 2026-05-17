# 从零实现 Coding Agent（二十六）：沙箱与安全隔离

前面第 10 篇实现了权限系统——在工具执行**之前**拦截高危操作。但权限系统是"防君子不防小人"的：它挡住了 LLM 明确请求的危险操作，却挡不住**间接攻击**。

一个典型例子：

```
用户: 帮我分析这个项目
Agent: bash("cat package.json")  # 权限通过——只是读文件
# 但如果 package.json 里有 postinstall 脚本，触发了 npm install...
# 或者 .bashrc 被 source，执行了恶意 curl...
```

权限系统只看"你想做什么"，沙箱看"你实际做了什么"。两者是互补关系：

| | 权限系统（第 10 篇） | 沙箱（本篇） |
|---|---|---|
| 层级 | 应用层 | 操作系统层 |
| 时机 | 执行前拦截 | 执行时限制 |
| 粒度 | 工具名+参数模式 | 系统调用+路径+网络 |
| 防御目标 | LLM 误操作 | 间接攻击、提权、逃逸 |

这一篇拆解 Claude Code 的沙箱设计，然后给出一个 Linux 下的简化实现方案。

---

## Claude Code 的沙箱架构

Claude Code 的沙箱系统分三层：

```
settings.json (用户配置)
      ↓
sandbox-adapter.ts (转换层)
      ↓
@anthropic-ai/sandbox-runtime (运行时)
      ↓
bubblewrap (bwrap) / macOS sandbox-exec (OS 隔离)
```

核心包是 `@anthropic-ai/sandbox-runtime`，Claude Code 通过 `sandbox-adapter.ts` 做适配，把自身的 settings 格式转成 `SandboxRuntimeConfig`。

### 启用条件

沙箱不是默认开启的，需要同时满足：

1. `settings.json` 中 `sandbox.enabled: true`
2. 平台支持（macOS / Linux / WSL2+，不支持 WSL1）
3. 平台在 `enabledPlatforms` 列表中（可选限制）
4. 依赖可用（`bwrap` 命令存在）

```typescript
// Claude Code: utils/sandbox/sandbox-adapter.ts
function isSandboxingEnabled(): boolean {
  if (!isSupportedPlatform()) return false
  if (!isPlatformInEnabledList()) return false
  return getSandboxEnabledSetting()
}
```

### 三维隔离

沙箱从三个维度限制命令执行：

#### 1. 文件系统写限制

```typescript
// 默认允许写的路径
const allowWrite: string[] = ['.', getClaudeTempDir()]

// 始终禁止写的路径（防止沙箱逃逸）
denyWrite.push(...settingsPaths)        // settings.json 文件
denyWrite.push(resolve(cwd, '.claude', 'skills'))  // skills 目录
```

核心安全原则：**Agent 不能修改自己的配置文件和 skill 定义**。如果 Agent 能改 settings.json，就能关掉沙箱；如果能改 skills/，就能注入恶意工作流。

还有一个精巧的防御——防止伪造 git bare repo 逃逸：

```typescript
// SECURITY: 防止攻击者在 cwd 植入 HEAD + objects/ + refs/
// 让 git 误认为这是 bare repo，通过 core.fsmonitor 执行任意命令
const bareGitRepoFiles = ['HEAD', 'objects', 'refs', 'hooks', 'config']
for (const gitFile of bareGitRepoFiles) {
  const p = resolve(dir, gitFile)
  if (existsSync(p)) {
    denyWrite.push(p)   // 已存在 → 只读挂载
  } else {
    bareGitRepoScrubPaths.push(p)  // 不存在 → 命令结束后清理
  }
}
```

#### 2. 文件系统读限制

与写限制类似，可以配置 `denyRead` 列表，阻止命令读取敏感文件（如 `.env`、SSH 密钥）。

#### 3. 网络限制

```typescript
// 从 WebFetch 权限规则中提取允许的域名
for (const ruleString of permissions.allow || []) {
  const rule = permissionRuleValueFromString(ruleString)
  if (rule.toolName === 'WebFetch' && rule.ruleContent?.startsWith('domain:')) {
    allowedDomains.push(rule.ruleContent.substring('domain:'.length))
  }
}
```

网络限制与权限系统联动：如果用户允许了 `WebFetch(domain:github.com)`，沙箱也放行对 `github.com` 的网络访问。

### 路径解析

Claude Code 的路径配置有三种语法：

| 语法 | 含义 | 示例 |
|------|------|------|
| `//path` | 绝对路径（从根开始） | `//.aws/**` → `/.aws/**` |
| `/path` | 相对于 settings 文件目录 | `/src/**` → `${settingsDir}/src/**` |
| `~/path` | 用户 home 目录 | `~/.cargo/**` |

```typescript
export function resolvePathPatternForSandbox(pattern: string, source: SettingSource): string {
  if (pattern.startsWith('//')) return pattern.slice(1)
  if (pattern.startsWith('/')) {
    const root = getSettingsRootPathForSource(source)
    return resolve(root, pattern.slice(1))
  }
  return pattern  // ~/path, ./path 直接透传给 sandbox-runtime
}
```

### `--dangerously-skip-permissions` 的安全阀

Claude Code 允许跳过权限（用于 CI/自动化），但有严格前提：

```typescript
// Claude Code: setup.ts
const isDocker = envDynamic.getIsDocker()
const isBubblewrap = envDynamic.getIsBubblewrapSandbox()
const isSandbox = process.env.IS_SANDBOX === '1'
const isSandboxed = isDocker || isBubblewrap || isSandbox

if (!isSandboxed || hasInternet) {
  throw new Error(
    '--dangerously-skip-permissions can only be used in ' +
    'Docker/sandbox containers with no internet access'
  )
}
```

逻辑清晰：**只有在已经被容器隔离且无网络的环境中，才允许跳过权限**。双重保险。

### 违规记录

沙箱不是默默拦截——它记录每次违规尝试：

```typescript
type SandboxViolationEvent = {
  type: 'fs_read' | 'fs_write' | 'network'
  path?: string
  host?: string
  timestamp: number
}
```

`SandboxViolationStore` 收集这些事件，可以用于：
- 向用户展示被拦截的操作
- 分析 Agent 是否在尝试越权
- 调整沙箱配置（把频繁被拦的合法路径加入白名单）

### autoAllowBashIfSandboxed

一个精巧的 UX 优化：当沙箱启用时，bash 命令**自动允许执行**（无需每次弹出权限询问），因为沙箱已经在 OS 层面保证安全了：

```typescript
function isAutoAllowBashIfSandboxedEnabled(): boolean {
  return settings?.sandbox?.autoAllowBashIfSandboxed ?? true  // 默认开启
}
```

这解决了权限系统的一个痛点：频繁弹窗确认 bash 命令太烦了。有沙箱兜底后，权限系统可以放松 bash 的控制，体验更流畅。

---

## 简化实现

我们用 Linux 下的 `bubblewrap` (bwrap) 来实现核心隔离。macOS 用户可以用 `sandbox-exec`（但功能较弱）。

### 沙箱配置

```python
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SandboxConfig:
    """沙箱配置"""
    enabled: bool = False
    # 文件系统
    allow_write: list[str] = field(default_factory=lambda: ["."])
    deny_write: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    # 网络
    allow_network: bool = True
    allowed_hosts: list[str] = field(default_factory=list)
    # 行为
    auto_allow_bash: bool = True          # 沙箱启用时自动允许 bash
    fail_if_unavailable: bool = False     # bwrap 不存在时是否报错


# 始终禁止写入的路径（防逃逸）
ALWAYS_DENY_WRITE = [
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/skills",
]
```

### 命令包装

核心思路：把用户命令包装在 `bwrap` 调用中。

```python
import shutil
import subprocess
from pathlib import Path


def is_bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def build_sandbox_command(
    command: str,
    cwd: str,
    config: SandboxConfig,
) -> list[str]:
    """把原始命令包装为 bwrap 沙箱命令"""
    args = ["bwrap"]

    # 基础文件系统：只读绑定整个根目录
    args += ["--ro-bind", "/", "/"]

    # 必要的可写路径
    args += ["--dev", "/dev"]
    args += ["--proc", "/proc"]
    args += ["--tmpfs", "/tmp"]

    # 允许写入的路径
    all_allow = _resolve_paths(config.allow_write, cwd)
    for path in all_allow:
        if Path(path).exists():
            args += ["--bind", path, path]

    # 禁止写入的路径（覆盖上面的允许规则）
    all_deny = _resolve_paths(
        config.deny_write + ALWAYS_DENY_WRITE, cwd
    )
    for path in all_deny:
        if Path(path).exists():
            args += ["--ro-bind", path, path]

    # 网络隔离
    if not config.allow_network:
        args += ["--unshare-net"]

    # 工作目录
    args += ["--chdir", cwd]

    # 执行命令
    args += ["--", "bash", "-c", command]

    return args


def _resolve_paths(patterns: list[str], cwd: str) -> list[str]:
    """解析路径模式"""
    resolved = []
    for p in patterns:
        if p == ".":
            resolved.append(cwd)
        elif p.startswith("~/"):
            resolved.append(str(Path.home() / p[2:]))
        elif p.startswith("/"):
            resolved.append(p)
        else:
            resolved.append(str(Path(cwd) / p))
    return resolved
```

### 集成到 BashTool

```python
def execute_command(
    command: str,
    cwd: str,
    timeout: int,
    sandbox_config: SandboxConfig,
) -> subprocess.CompletedProcess:
    """执行命令，根据配置决定是否走沙箱"""

    if sandbox_config.enabled and is_bwrap_available():
        args = build_sandbox_command(command, cwd, sandbox_config)
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
    elif sandbox_config.enabled and sandbox_config.fail_if_unavailable:
        raise RuntimeError("沙箱已启用但 bwrap 不可用")
    else:
        # 回退到无沙箱执行
        return subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
```

### 违规检测

bwrap 在拒绝操作时会返回 EPERM 错误。我们可以解析 stderr 来检测：

```python
import re

_VIOLATION_PATTERNS = [
    (re.compile(r"Permission denied.*?'(.+)'"), "fs_write"),
    (re.compile(r"Read-only file system.*?'(.+)'"), "fs_write"),
    (re.compile(r"Network is unreachable"), "network"),
]


@dataclass
class SandboxViolation:
    type: str       # "fs_write" | "fs_read" | "network"
    path: str = ""
    timestamp: float = 0.0


def detect_violations(stderr: str) -> list[SandboxViolation]:
    """从 stderr 中检测沙箱违规"""
    violations = []
    for pattern, vtype in _VIOLATION_PATTERNS:
        for match in pattern.finditer(stderr):
            violations.append(SandboxViolation(
                type=vtype,
                path=match.group(1) if match.lastindex else "",
                timestamp=time.time(),
            ))
    return violations
```

---

## 完整配置示例

```json
{
  "sandbox": {
    "enabled": true,
    "autoAllowBashIfSandboxed": true,
    "failIfUnavailable": false,
    "filesystem": {
      "allowWrite": [".", "~/.cache/pip"],
      "denyWrite": [".env", ".env.local"],
      "denyRead": ["~/.ssh/id_rsa"]
    },
    "network": {
      "allowedDomains": ["github.com", "pypi.org", "registry.npmjs.org"]
    }
  }
}
```

---

## 设计决策

### 为什么用 bwrap 而不是 Docker？

| | Docker | bwrap |
|---|---|---|
| 启动开销 | ~200ms（daemon RPC） | ~2ms（直接 fork） |
| 适用场景 | 长期运行的隔离环境 | 短命令的轻量隔离 |
| 共享文件系统 | 需要 volume mount | 直接 bind mount |
| 网络隔离 | 默认完整隔离 | 可选（`--unshare-net`） |

Agent 的 bash 命令大多是 `ls`、`cat`、`grep` 这种毫秒级操作，Docker 200ms 的启动开销不可接受。bwrap 几乎零开销，适合逐命令包装。

### 为什么 deny 优先于 allow？

沙箱的 deny 规则**总是覆盖** allow 规则（在 bwrap 中，后绑定的 `--ro-bind` 覆盖前面的 `--bind`）。这是安全领域的标准做法：白名单可能有漏洞，但黑名单是最后防线。

### 为什么 settings.json 必须禁止写入？

如果 Agent 能修改 settings.json，它可以：
1. 关闭沙箱 → 获得完整系统权限
2. 添加 allow 规则 → 绕过权限检查
3. 修改 hooks → 注入恶意逻辑

所以 Claude Code 在 OS 层面 hard-code 了对 settings 文件的写保护，这条规则不受任何配置影响。

---

## 与 Claude Code 的对比

| 维度 | Claude Code | 我们的实现 |
|------|-------------|------------|
| 隔离后端 | `@anthropic-ai/sandbox-runtime`（bwrap + macOS sandbox-exec） | 直接调用 bwrap |
| 平台支持 | macOS + Linux + WSL2 | 仅 Linux（bwrap） |
| 文件系统控制 | read/write 分离、per-source 路径解析 | 统一 deny/allow 列表 |
| 网络控制 | 域名级白名单（与 WebFetch 联动） | 全开/全关二选一 |
| 配置来源 | 多层 settings 合并（user/project/policy） | 单一配置对象 |
| 违规处理 | ViolationStore + UI 展示 | stderr 解析 + 日志 |
| git 防逃逸 | bare repo 检测 + 文件清理 | 未实现 |
| 代码量 | ~600 行 TypeScript（adapter 层） | ~80 行 Python |

---

## 小结

沙箱是 Agent 安全的**最后一道防线**：

- **权限系统**是应用层的前置检查——"你不该做这个"
- **沙箱**是操作系统层的强制隔离——"就算你想做，也做不到"

核心设计思想：
1. **最小权限**——默认只读，显式开放写入
2. **防逃逸**——保护自身配置文件和 skill 定义
3. **与权限系统联动**——沙箱启用后可放松权限询问，提升 UX
4. **违规可观测**——拦截不是终点，记录才能改进

下一篇我们来实现**可观测性与 Tracing**——让 Agent 的每一步行为都可追踪、可度量、可回溯。
