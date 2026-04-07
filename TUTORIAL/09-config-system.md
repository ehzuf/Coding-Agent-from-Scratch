# 从零实现 Coding Agent（九）：配置系统 + CLI 完善

随着功能增加，配置项也越来越多。我们需要一个统一的配置系统来管理它们。

## 配置的来源

一个成熟的应用通常有多个配置来源：

1. **命令行参数**：临时覆盖，优先级最高
2. **环境变量**：系统级配置，适合 CI/CD
3. **配置文件**：项目级配置，适合团队共享
4. **代码默认值**：保底配置

我们的实现支持前三种（配置文件预留扩展）。

## 配置类设计

```python
# agent/config.py

from dataclasses import dataclass

@dataclass
class Config:
    """Agent 配置"""

    # LLM 配置
    provider: str = "anthropic"
    model: str | None = None
    base_url: str | None = None

    # Agent 行为配置
    max_turns: int = 20
    enable_budget: bool = True
    enable_compact: bool = True
    enable_retry: bool = True
    max_retries: int = 3

    # 输出配置
    stream: bool = True
    verbose: bool = True

    # 路径配置
    cwd: str = "."
    system_prompt: str | None = None
```

使用 dataclass 的好处：
- 自动生成 `__init__`、`__repr__` 等方法
- 类型注解清晰
- 可以方便地转换为字典

## 配置加载优先级

```python
@classmethod
def from_env(cls) -> "Config":
    """从环境变量加载配置"""
    config = cls()

    # LLM 配置
    config.provider = os.environ.get("LLM_PROVIDER", config.provider)
    config.model = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("OPENAI_MODEL")

    # 数值配置
    if max_turns := os.environ.get("AGENT_MAX_TURNS"):
        config.max_turns = int(max_turns)

    # 布尔配置
    if os.environ.get("AGENT_NO_BUDGET"):
        config.enable_budget = False

    return config

@classmethod
def from_args(cls, args: Any) -> "Config":
    """从命令行参数加载配置（覆盖环境变量）"""
    config = cls.from_env()

    if hasattr(args, "provider") and args.provider:
        config.provider = args.provider
    # ... 其他参数

    return config
```

## 环境变量设计

环境变量命名规范：
- `LLM_PROVIDER`：通用配置，无前缀
- `ANTHROPIC_MODEL`：提供商特定
- `AGENT_MAX_TURNS`：agent 特定，加 `AGENT_` 前缀

布尔值环境变量：
- 使用 `AGENT_NO_XXX` 格式
- 存在即禁用（无需赋值）
- 例如：`AGENT_NO_BUDGET=1` 或只是 `AGENT_NO_BUDGET=`

完整列表：

| 环境变量 | 说明 |
|----------|------|
| `LLM_PROVIDER` | 默认 LLM 后端 |
| `ANTHROPIC_MODEL` | Anthropic 模型 |
| `OPENAI_MODEL` | OpenAI 模型 |
| `ANTHROPIC_BASE_URL` | Anthropic API 地址 |
| `OPENAI_BASE_URL` | OpenAI API 地址 |
| `AGENT_MAX_TURNS` | 最大 turn 数 |
| `AGENT_MAX_RETRIES` | 最大重试次数 |
| `AGENT_NO_BUDGET` | 禁用 budget |
| `AGENT_NO_COMPACT` | 禁用 compact |
| `AGENT_NO_RETRY` | 禁用重试 |
| `AGENT_NO_STREAM` | 禁用流式 |

> **注意**：`ANTHROPIC_BASE_URL` 和 `OPENAI_BASE_URL` 的选择取决于 `LLM_PROVIDER` 或 `--provider` 参数。当 `provider=anthropic` 时，只读取 `ANTHROPIC_BASE_URL`；当 `provider=openai` 时，只读取 `OPENAI_BASE_URL`。这避免了同时配置多个兼容服务时的冲突。

## CLI 参数设计

```python
parser.add_argument("--provider", choices=["anthropic", "openai"])
parser.add_argument("--model")
parser.add_argument("--base-url")
parser.add_argument("--no-stream", action="store_true")
parser.add_argument("--cwd", default=".")
parser.add_argument("--system")
parser.add_argument("--max-turns", type=int)
parser.add_argument("--no-budget", action="store_true")
parser.add_argument("--no-compact", action="store_true")
parser.add_argument("--no-retry", action="store_true")
```

设计原则：
- 长参数名使用 `--kebab-case`
- 布尔标志使用 `--no-xxx` 格式（禁用某功能）
- 提供合理的默认值
- 详细的帮助信息

## 使用示例

### 环境变量配置

```bash
# ~/.bashrc 或 ~/.zshrc
export LLM_PROVIDER=openai
export OPENAI_MODEL=gpt-4o
export AGENT_MAX_TURNS=10
```

### 命令行覆盖

```bash
# 临时使用 anthropic
python -m agent --provider anthropic "你好"

# 禁用流式输出用于调试
python -m agent --no-stream "debug"

# 限制 turn 数防止无限循环
python -m agent --max-turns 5 "复杂任务"
```

### 同时使用多个兼容服务

```bash
# 同时配置 Anthropic 和 OpenAI 兼容服务（如百炼）
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_BASE_URL=https://dashscope.aliyuncs.com/apps/anthropic/
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# 使用 Anthropic 后端（Claude 模型）
python -m agent --provider anthropic --model claude-sonnet-4 "你好"

# 使用 OpenAI 后端（通义千问模型）
python -m agent --provider openai --model qwen-plus "你好"
```

> 注意：即使同时设置了 `ANTHROPIC_BASE_URL` 和 `OPENAI_BASE_URL`，系统也会根据 `--provider` 参数自动选择正确的地址。

### Python 代码中使用

```python
from agent.config import Config, load_config

# 纯环境变量
config = Config.from_env()

# 混合（命令行优先）
config = load_config(args)

# 手动创建
config = Config(
    provider="anthropic",
    model="claude-sonnet-4",
    max_turns=30,
)
```

## 配置验证

生产环境应该添加配置验证：

```python
def validate_config(config: Config) -> None:
    """验证配置有效性"""
    if config.max_turns < 1:
        raise ValueError("max_turns 必须 >= 1")

    if config.provider not in ["anthropic", "openai"]:
        raise ValueError(f"不支持的 provider: {config.provider}")

    # ... 更多验证
```

## 配置文件的扩展

未来可以添加配置文件支持：

```yaml
# ~/.coding-agent/config.yaml
provider: anthropic
model: claude-sonnet-4
max_turns: 20
enable_budget: true
enable_compact: true

# 项目级配置
# .coding-agent.yaml
cwd: .
system_prompt: "你是一个 Python 专家"
```

加载优先级：
1. 命令行参数
2. 项目级配置文件（./.coding-agent.yaml）
3. 用户级配置文件（~/.coding-agent/config.yaml）
4. 环境变量
5. 默认值

## 这一步我们学到了什么

1. **配置分层**：不同来源适合不同场景
2. **优先级明确**：命令行 > 环境变量 > 默认值
3. **命名规范**：环境变量使用统一前缀避免冲突
4. **可扩展性**：预留配置文件接口，方便未来扩展

配置系统是产品化的重要一步。它让用户可以自定义行为，而不必修改代码。

下一篇将实现权限系统——在 Agent 执行危险工具前进行安全拦截和人工确认。
