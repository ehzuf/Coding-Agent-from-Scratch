# Python Coding Agent 开发文档

从零实现一个最精简的 coding agent，逐步对标 Claude Code（TypeScript 版）的核心功能，深入理解 coding agent 的实现原理。

---

## 环境准备

### 创建虚拟环境（推荐）

虚拟环境将项目依赖与系统 Python 隔离，避免版本冲突。

```bash
cd coding-agent-from-scratch

# 创建虚拟环境（只需执行一次）
python -m venv .venv

# 激活（每次开新终端都需要执行）
source .venv/bin/activate       # macOS / Linux
# .venv\Scripts\activate        # Windows

# 安装项目及依赖
pip install -e .

# 退出虚拟环境
deactivate
```

激活后，终端提示符会显示 `(.venv)`，表示当前在虚拟环境中。

### 环境变量

| 变量 | 说明 | 是否必须 |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic / 兼容代理的 API 密钥 | 是 |
| `ANTHROPIC_BASE_URL` | Anthropic 兼容代理地址 | 使用代理时需要 |
| `ANTHROPIC_MODEL` | Anthropic 后端默认模型，如 `claude-sonnet-4-20250514` | 否 |
| `OPENAI_API_KEY` | OpenAI 或兼容服务的 API 密钥 | 使用 OpenAI 后端时需要 |
| `OPENAI_BASE_URL` | OpenAI 兼容服务地址 | 使用兼容服务时需要 |
| `OPENAI_MODEL` | OpenAI 后端默认模型，如 `gpt-4o` | 否 |
| `LLM_PROVIDER` | CLI 默认后端，`anthropic` 或 `openai` | 否，默认 `anthropic` |

---

## 项目结构

```
coding-agent-from-scratch/
├── agent/
│   ├── __init__.py
│   ├── __main__.py        # CLI 入口
│   ├── agent.py           # Agent 类（消息历史 + Tool Use 循环）
│   ├── coordinator.py     # Coordinator 模式（系统提示 + task-notification）
│   ├── context.py         # 上下文管理（分层记忆文件加载 + @include + 系统提示组装）
│   ├── session.py         # 会话持久化（JSONL 存储 + resume 恢复）
│   ├── session_memory.py  # 会话记忆（定期 LLM 提取结构化笔记）
│   ├── auto_memory.py     # 跨会话记忆（持久化记忆提取 + MEMORY.md 索引）
│   ├── mcp_client.py      # MCP 客户端（连接 MCP Server + 工具代理）
│   ├── skills.py          # Skills 系统（自定义技能 + markdown prompt）
│   ├── hooks.py           # Hooks 系统（PreToolUse / PostToolUse / UserPromptSubmit）
│   ├── llm/
│   │   ├── __init__.py    # create_llm() 工厂函数
│   │   ├── base.py        # BaseLLM 抽象基类 + LLMResponse
│   │   ├── anthropic_llm.py
│   │   └── openai_llm.py
│   └── tools/
│       ├── __init__.py    # 工具注册表
│       ├── base.py        # Tool 抽象基类
│       ├── get_current_time.py  # Demo 工具
│       ├── bash.py        # Bash 工具
│       ├── read.py        # 文件读取工具
│       ├── write.py       # 文件写入工具
│       ├── edit.py        # 文件编辑工具
│       ├── glob.py        # 文件路径匹配工具
│       ├── grep.py        # 内容搜索工具
│       ├── agent_tool.py  # 子 Agent 工具（运行时注入）
│       ├── send_message.py # SendMessage 工具（运行时注入）
│       └── plan_mode.py   # Plan Mode 工具（EnterPlanMode + ExitPlanMode）
├── reference/             # 参考实现（Claude Code TypeScript 源码）
├── pyproject.toml
└── requirements.txt
```

---

## 如何测试

### Step 1 & 2：LLM 调用 + 流式输出

**流式输出（默认，打字机效果）：**
```bash
python -m agent "用三句话介绍 Python"
```

**非流式输出（等待完整回复，显示 token 用量）：**
```bash
python -m agent --no-stream "用三句话介绍 Python"
```

**指定模型：**
```bash
python -m agent --model claude-sonnet-4-20250514 "你好"
```

**使用 OpenAI 兼容后端（base-url 从环境变量读取）：**
```bash
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_MODEL=gpt-4o

python -m agent --provider openai "你好"
```

**临时覆盖，不改环境变量：**
```bash
python -m agent --provider openai --model gpt-4o "你好"
```

**在 Python 代码里直接调用：**
```python
from agent.llm import create_llm

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')

# 非流式
r = llm.chat([{'role': 'user', 'content': '你好'}])
print(r.content)           # 回复文本
print(r.input_tokens)      # 输入 token 数
print(r.output_tokens)     # 输出 token 数

# 流式
for chunk in llm.stream([{'role': 'user', 'content': '你好'}]):
    print(chunk, end='', flush=True)

# 带 system prompt
r = llm.chat(
    messages=[{'role': 'user', 'content': '你好'}],
    system='你是一个简洁的助手，每次回复不超过 20 字。',
)
```

**OpenAI 兼容服务（代码调用）：**
```python
import os
from agent.llm import create_llm

llm = create_llm(
    provider='openai',
    model='gpt-4o',
    api_key=os.environ['OPENAI_API_KEY'],
    base_url='https://api.openai.com/v1',
)
r = llm.chat([{'role': 'user', 'content': '你好'}])
print(r.content)
```

### Step 3：多轮对话

**交互式 REPL（不传 prompt 参数）：**
```bash
python -m agent
```

进入后支持以下指令：
- `/clear` — 清空消息历史，开始新对话
- `/exit` 或 `/quit` — 退出
- `Ctrl+C` — 退出

**在 Python 代码里使用 Agent：**
```python
from agent.llm import create_llm
from agent.agent import Agent
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
tools = get_tools()  # 获取内置工具
agent = Agent(llm=llm, tools=tools, system="你是一个简洁的助手")

# 多轮对话（非流式）
r1 = agent.chat("我叫小明")
r2 = agent.chat("我叫什么名字？")   # LLM 能记住"小明"
print(r2.text)

# 查看状态
print(agent.turn_count)          # 已完成轮数
print(len(agent.messages))       # 历史消息总条数

# 清空历史
agent.clear()
```

### Step 4：Tool Use

**单次问答（支持工具调用）：**
```bash
python -m agent "现在几点了？北京时间"
```

LLM 会自动判断是否需要调用工具（如 `get_current_time`），Agent 执行工具后将结果回传给 LLM，最终生成回复。

**流式输出（支持 Tool Use）：**
```bash
python -m agent --stream "现在几点了？北京时间"
```

输出示例：
```
[get_current_time] 现在是北京时间 2026 年 4 月 3 日 15:13:06。
```

工具调用会以灰色 `[tool_name]` 内联显示，LLM 继续输出文本。

**在 Python 代码中使用流式 Tool Use：**
```python
from agent.llm import create_llm
from agent.agent import Agent, StreamEvent
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
agent = Agent(llm=llm, tools=get_tools())

for event in agent.stream("现在几点了？"):
    if event.type == "text":
        print(event.text, end="", flush=True)
    elif event.type == "tool_use_start":
        print(f"[调用工具: {event.name}]")
    elif event.type == "tool_use_end":
        print(f"结果: {event.result}")
```

**自定义工具：**
```python
from agent.tools.base import Tool

class MyTool(Tool):
    @property
    def name(self) -> str:
        return "my_tool"

    @property
    def description(self) -> str:
        return "工具描述（给 LLM 看的）"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "arg1": {"type": "string", "description": "参数说明"},
            },
            "required": ["arg1"],
        }

    def call(self, input: dict) -> str:
        # 执行工具逻辑，返回结果字符串
        return f"处理结果: {input['arg1']}"

# 使用自定义工具
from agent.llm import create_llm
from agent.agent import Agent

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
agent = Agent(llm=llm, tools=[MyTool()])
r = agent.chat("请调用 my_tool 处理 xxx")
print(r.text)
```

### Step 5：文件操作工具

**列出当前目录文件：**
```bash
python -m agent "列出当前目录的所有 Python 文件"
```

Agent 会自动调用 `glob` 工具查找文件。

**读取文件内容：**
```bash
python -m agent "读取 agent/tools/base.py 的内容"
```

**执行 shell 命令：**
```bash
python -m agent "运行 git status 查看状态"
```

**搜索代码：**
```bash
python -m agent "搜索所有包含 'Tool' 的 Python 文件"
```

Agent 会调用 `grep` 工具搜索内容。

**编辑文件（精确替换）：**
```bash
python -m agent "在 README.md 的第一行添加项目标题"
```

Agent 会先 `read` 文件，然后使用 `edit` 工具进行精确字符串替换。

**文件操作工作流示例：**
```
用户: 在 agent/tools/ 下创建一个新工具叫 hello.py
Agent: [glob] 查看现有工具结构
       [read] 读取 base.py 了解工具基类
       [write] 创建 hello.py 文件
       [bash] 运行 python -c "from agent.tools.hello import HelloTool; print('OK')" 验证
```

### Step 6：System Prompt + AGENTS.md

**创建 AGENTS.md 文件：**

在项目根目录创建 `AGENTS.md`：

```markdown
# 项目规范

## 技术栈
- Python 3.10+
- 使用 dataclasses 而非字典传参
- 类型注解是必需的

## 代码风格
- 函数名使用 snake_case
- 类名使用 PascalCase
- 优先使用 pathlib 而非 os.path

## 文件结构
- agent/llm/ - LLM 后端实现
- agent/tools/ - 工具实现
- agent/context.py - 上下文管理
```

**使用 AGENTS.md：**

```bash
# 在包含 AGENTS.md 的目录运行
python -m agent "这个项目的代码风格是什么？"
```

Agent 会自动加载 AGENTS.md 并了解项目规范。

**指定工作目录：**

```bash
# 从其他目录运行，指定项目路径
python -m agent --cwd /path/to/project "查看项目结构"
```

**添加额外系统提示：**

```bash
python -m agent --system "你是一个资深 Python 开发者" "review 这段代码"
```

**在 Python 代码中使用：**

```python
from agent.context import build_system_prompt, load_agents_md

# 加载 AGENTS.md
agents_content = load_agents_md("/path/to/project")
print(agents_content)

# 构建完整系统提示
system = build_system_prompt(
    base_system="你是一个助手",
    cwd="/path/to/project",
    include_date=True,
)
```

### Step 7：上下文预算管理

**Tool Result Budget（自动截断大输出）：**

```python
from agent.agent import Agent
from agent.llm import create_llm
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')

# 启用 budget 控制（默认启用）
agent = Agent(
    llm=llm,
    tools=get_tools(),
    enable_budget=True,
    max_tool_result_length=5000,  # 单个 tool result 最大 5000 字符
)

# 读取大文件时会自动截断
response = agent.chat("读取 /var/log/syslog 的最后 1000 行")
```

截断后的输出格式：
```
[文件开头内容]

... [内容已截断，省略 50000 字符，共 60000 字符] ...

[文件结尾内容]
```

**Auto Compact（自动压缩历史）：**

```python
from agent.agent import Agent
from agent.llm import create_llm
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')

# 启用自动压缩（默认启用）
agent = Agent(
    llm=llm,
    tools=get_tools(),
    enable_compact=True,
    compact_threshold=60000,  # token 数超过 60000 时触发压缩
)

# 进行多轮对话，历史会自动压缩
for i in range(100):
    response = agent.chat(f"第 {i} 轮对话...")
```

**手动检查上下文预算：**

```python
from agent.budget import check_context_budget

# 检查当前消息历史的预算使用情况
budget_info = check_context_budget(agent.messages, max_tokens=100000)
print(f"估算 token 数: {budget_info['total_tokens']}")
print(f"使用比例: {budget_info['usage_ratio']:.1%}")
print(f"是否警告: {budget_info['is_warning']}")
```

**手动压缩历史：**

```python
from agent.compact import compact_messages

# 手动触发压缩
result = compact_messages(
    agent.messages,
    llm=llm,
    keep_recent=4,  # 保留最近 4 轮
)
print(f"原始 token: {result.original_tokens}")
print(f"压缩后 token: {result.new_tokens}")
print(f"摘要: {result.summary[:200]}...")

# 更新消息历史
agent.messages = [...]  # 使用压缩后的消息
```

### Step 8：Max Turns 保护 + 错误处理

**配置 Max Turns 限制：**

```python
from agent.agent import Agent
from agent.llm import create_llm
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
agent = Agent(
    llm=llm,
    tools=get_tools(),
    max_turns=10,  # 限制最多 10 个 turns
)

# 如果工具调用形成无限循环，会在 10 轮后停止
response = agent.chat("请循环调用工具直到满足条件")
```

**启用 LLM 调用重试：**

```python
from agent.agent import Agent

agent = Agent(
    llm=llm,
    tools=get_tools(),
    enable_retry=True,
    max_retries=5,  # 最多重试 5 次
)

# 网络超时或连接错误时会自动重试
response = agent.chat("执行需要网络调用的任务")
```

**使用重试装饰器：**

```python
from agent.retry import with_retry

# 为任意函数添加重试机制
@with_retry(max_retries=3, initial_delay=1.0)
def fetch_data(url: str) -> str:
    # 可能失败的网络请求
    import urllib.request
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode('utf-8')

try:
    data = fetch_data("https://api.example.com/data")
except RetryError as e:
    print(f"请求失败: {e}")
```

**安全调用工具：**

```python
from agent.retry import safe_tool_call

# 包装可能失败的工具调用
result = safe_tool_call(
    risky_function,
    arg1, arg2,
    default_error_message="操作失败",
)
# 即使发生异常，也会返回错误信息字符串
```

### Step 9：配置系统 + CLI 完善

**使用环境变量配置：**

```bash
# 设置默认 LLM 后端
export LLM_PROVIDER=openai
export OPENAI_MODEL=gpt-4o

# 禁用某些功能
export AGENT_NO_BUDGET=1
export AGENT_NO_COMPACT=1

# 调整参数
export AGENT_MAX_TURNS=10
export AGENT_MAX_RETRIES=5

# 运行 agent
python -m agent "你好"
```

**使用命令行参数：**

```bash
# 指定模型和参数
python -m agent --provider anthropic --model claude-sonnet-4-20250514 "你好"

# 禁用流式输出
python -m agent --no-stream "用非流式回复"

# 设置最大 turn 数
python -m agent --max-turns 5 "执行复杂任务"

# 禁用重试（用于调试）
python -m agent --no-retry "测试网络错误处理"
```

**在 Python 代码中使用配置：**

```python
from agent.config import Config, load_config

# 从环境变量加载
config = Config.from_env()
print(f"Provider: {config.provider}")
print(f"Model: {config.model}")

# 从命令行参数加载（优先级更高）
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--provider")
args = parser.parse_args()

config = load_config(args)
```

**查看帮助信息：**

```bash
python -m agent --help
```

### Step 10：权限系统

**使用不同权限模式：**

```bash
# 默认模式（ask），危险操作会询问确认
python -m agent "删除临时文件"

# 宽松模式，自动允许所有操作
python -m agent --permission-mode allow "执行任意命令"

# 严格模式，只允许读取操作
python -m agent --permission-mode strict "读取配置文件"

# 完全禁用权限检查
python -m agent --no-permission "执行命令"
```

**权限确认交互（ask 模式）：**

```
[权限请求] 工具: bash
  参数: rm -rf /tmp/test
  允许执行? [y/n/a(yes to all)] y

[bash] 命令已执行
```

**在 Python 代码中使用权限系统：**

```python
from agent.permission import (
    PermissionManager,
    PermissionConfig,
    PermissionRule,
    PermissionMode,
    get_strict_permission_config,
)

# 使用预设配置
config = get_strict_permission_config()
manager = PermissionManager(config)

# 检查权限
allowed, reason = manager.should_execute("bash", "git status")
print(f"允许: {allowed}, 原因: {reason}")

# 自定义规则
custom_config = PermissionConfig(
    default_mode=PermissionMode.ASK,
    allow_rules=[
        PermissionRule.parse("bash(git *)", PermissionMode.ALLOW),
        PermissionRule.parse("read(*)", PermissionMode.ALLOW),
    ],
    deny_rules=[
        PermissionRule.parse("bash(rm *)", PermissionMode.DENY),
    ],
)
manager = PermissionManager(custom_config)
```

**环境变量配置：**

```bash
# 设置权限模式
export AGENT_PERMISSION_MODE=strict

# 禁用权限检查（不推荐）
export AGENT_NO_PERMISSION=1
```

### Step 11：Prompt Caching

**使用 Prompt Caching（默认开启）：**

```bash
# 默认启用 prompt caching，流式和非流式都会显示 cache 用量
python -m agent "介绍一下这个项目的代码结构"
python -m agent --no-stream "列出所有 Python 文件"
```

输出示例（第二次请求命中缓存）：
```
--- token 用量: 输入 120, 输出 85, 缓存命中 3200 ---
```

**查看缓存分析：**

```bash
# 单次模式：请求结束后显示缓存分析
python -m agent --show-cache "介绍一下项目"

# 交互模式：输入 /cache 查看当前缓存状态
python -m agent
[1] 你: 你好
[2] 你: /cache
```

输出示例：
```
=== 缓存分析 ===

后端: Anthropic（手动标记 cache_control）

System prompt: ~500 tokens
  → 已加 cache_control 标记，每次请求都会命中缓存
Tools 定义:    ~983 tokens（7 个工具）
  → 工具列表不变，自动参与缓存

消息历史: 6 条，~850 tokens

消息分布:
  [ 0] user     : ~   15 tokens [已缓存]
  [ 1] assistant: ~  200 tokens [已缓存]
  [ 2] user     : ~   10 tokens [已缓存]
  [ 3] assistant: ~  500 tokens [已缓存]
  [ 4] user     : ~   20 tokens [已缓存]
  [ 5] assistant: ~  105 tokens ← cache_control（此消息及之前被缓存）

缓存策略:
  已缓存: ~1833 tokens（system 500 + tools 983 + 历史 850）
  下次请求时，仅新输入的 user 消息不在缓存中

费用参考:
  缓存命中: 节省 90% 输入费用（仅付 10%）
  缓存写入: 额外 25% 费用（首次建立）
  缓存 TTL: 5 分钟（每次命中重置计时）
```

**在 Python 代码中使用：**

```python
from agent.llm import create_llm

# Anthropic 后端：默认开启 cache_control 标记
llm = create_llm('anthropic', 'claude-opus-4-5', enable_cache=True)

r1 = llm.chat(
    messages=[{'role': 'user', 'content': '你好'}],
    system='这是一个很长的系统提示...',
)
print(f"第一次 - 缓存写入: {r1.cache_write_tokens}, 命中: {r1.cache_read_tokens}")

r2 = llm.chat(
    messages=[{'role': 'user', 'content': '继续'}],
    system='这是一个很长的系统提示...',
)
print(f"第二次 - 缓存命中: {r2.cache_read_tokens}")  # 第二次命中

# 禁用 caching（调试用）
llm_no_cache = create_llm('anthropic', 'claude-opus-4-5', enable_cache=False)
```

**LLMResponse 中的 cache 字段：**

```python
response = agent.chat("问题")
print(response.input_tokens)       # 未命中缓存的输入 token（正常计费）
print(response.cache_read_tokens)  # 命中缓存读取的 token（折扣计费）
print(response.cache_write_tokens) # 写入缓存的 token（首次建立缓存，略贵）
print(response.output_tokens)      # 输出 token
```

---

## 实现进度

### 已完成

#### Step 1 — 项目骨架 + 单轮 LLM 调用
**对应 Claude Code 源码：** `services/api/claude.ts`

核心内容：
- `BaseLLM` 抽象基类，定义 `chat()` 和 `stream()` 两个统一接口
- `AnthropicLLM`：适配 Anthropic SDK，处理 system prompt 为独立参数、ThinkingBlock 过滤等差异
- `OpenAILLM`：适配 OpenAI SDK，兼容所有 OpenAI 协议服务（DeepSeek、Ollama 等）
- `create_llm()` 工厂函数：根据 provider 自动选择后端，上层代码不感知具体实现
- CLI 入口：`python -m agent "问题"`

两个后端的关键差异：

| | Anthropic | OpenAI |
|---|---|---|
| system prompt | 独立参数 `system=` | `role="system"` 消息插入 messages 首位 |
| token 字段名 | `input_tokens` / `output_tokens` | `prompt_tokens` / `completion_tokens` |
| 扩展参数 | `thinking={"type":"disabled"}` | `extra_body={"enable_thinking": False}` |

**两个后端都已支持 Tool Use**，但消息格式不同：
- Anthropic: `tool_result` 作为 user 消息的 content block
- OpenAI: 独立的 `role: "tool"` 消息

#### Step 2 — 流式输出
**对应 Claude Code 源码：** `query.ts` 中的 `for await (const message of deps.callModel(...))` 流式循环

核心内容：
- `stream()` 返回 `Iterator[str]`，逐块 yield 文本片段
- Anthropic 使用 `messages.stream()` 上下文管理器，`text_stream` 自动过滤非文本块
- OpenAI 使用 `stream=True` 参数，手动过滤 `delta.content is None` 的 chunk
- CLI 默认使用流式，`--no-stream` 切换为非流式
- `print(chunk, end='', flush=True)` —— `flush=True` 是关键，确保每块立即写入终端

#### Step 3 — 消息历史 + 多轮对话
**对应 Claude Code 源码：** `QueryEngine.ts` 中的 `mutableMessages` + REPL 主循环

核心内容：
- `Agent` 类维护 `self.messages: list[dict]`，每轮追加用户输入和 LLM 回复
- `chat(prompt)` — 非流式一轮对话，返回 `LLMResponse`
- `stream(prompt)` — 流式一轮对话，yield 文本片段，流结束后更新历史
- `clear()` — 清空历史，`turn_count` 属性查询已完成轮数
- CLI 新增交互模式：无参数运行进入 REPL，支持 `/clear` `/exit` 指令

多轮对话的本质：LLM 是无状态的，每次请求必须把完整历史消息列表一并发送。
`Agent` 的唯一职责就是维护这个列表，这也是所有 LLM 对话系统的基础机制。

#### Step 4 — Tool Use 协议
**对应 Claude Code 源码：** `Tool.ts` + `services/tools/toolOrchestration.ts`

核心内容：
- `Tool` 抽象基类：`name`、`description`、`input_schema`、`call(input) -> str`
- `LLMResponse` 扩展为 `content: list[dict]`，支持 `text` 和 `tool_use` 两种 block
- Agent 实现 query loop：`LLM.chat()` → 有 tool_use? → 执行 → 回传 → 继续
- `max_turns` 参数防止无限循环
- Demo 工具 `get_current_time`：验证完整流程
- **两个后端都已支持 Tool Use**（Anthropic 和 OpenAI 格式）

Anthropic vs OpenAI 消息格式差异：

| | Anthropic | OpenAI |
|---|---|---|
| 工具定义 | `input_schema` | `parameters`（包在 function 里）|
| 请求工具 | content block: `tool_use` | `tool_calls[].function` |
| 回传结果 | `role: "user"` + `tool_result` block | `role: "tool"` 独立消息 |

Tool Use 的本质：
```
while turn < max_turns:
    response = LLM.chat(messages, tools=工具列表)
    if response.stop_reason == "end_turn":
        return response.text  # LLM 直接回答
    elif response.has_tool_use:
        for tool_use in response.tool_uses:
            result = tool.call(tool_use.input)
            messages.append(tool_result)
        continue  # 下一轮
```

这是 coding agent 的核心能力——让 LLM 能够"操作"外部世界。

#### Step 5 — 文件操作工具组
**对应 Claude Code 源码：** `tools/BashTool/` + `tools/FileReadTool/` + `tools/FileWriteTool/` + `tools/FileEditTool/` + `tools/GlobTool/` + `tools/GrepTool/`

核心内容：
- `bash`：执行 shell 命令，支持超时和工作目录设置
- `read`：读取文件，支持多编码自动检测、offset/limit 分页
- `write`：写入/覆盖文件，自动创建父目录
- `edit`：精确字符串替换编辑（优于行号，更稳定）
- `glob`：文件路径模式匹配，支持 `**` 递归
- `grep`：正则表达式内容搜索，支持文件类型过滤

文件操作工具设计要点：
1. **字符串替换优于行号**：代码修改后行号会变化，字符串匹配更稳定
2. **Glob + Grep 是定位工具**：快速找到需要修改的文件和位置
3. **Edit 是精确修改工具**：只修改特定部分，不重写整个文件
4. **Bash 是万能工具**：但也是最危险的，需要权限控制

#### Step 6 — System Prompt + AGENTS.md
**对应 Claude Code 源码：** `context.ts` + `utils/queryContext.ts`

核心内容：
- 从工作目录向上递归查找 `AGENTS.md`
- 注入当前日期时间、工作目录等上下文信息
- 合并基础系统提示、AGENTS.md 内容、上下文信息

AGENTS.md 的作用：
- 项目特定的编码规范
- 技术栈说明
- 文件结构介绍
- 开发约定

CLI 新增参数：
- `--cwd`: 指定工作目录，用于查找 AGENTS.md
- `--system`: 额外的系统提示，与 AGENTS.md 合并

#### Step 7 — 上下文预算管理
**对应 Claude Code 源码：** `utils/toolResultStorage.ts` + `services/compact/autoCompact.ts`

核心内容：
- **Tool Result Budget**：单个 tool result 超过阈值时自动截断
  - 默认阈值：10000 字符
  - 策略：保留开头和结尾，中间用省略号替代
  - 防止大输出（如日志文件、长列表）撑爆上下文窗口

- **Auto Compact**：消息历史接近上限时自动压缩
  - 默认阈值：80000 tokens
  - 策略：保留最近 N 轮完整对话，对更早历史生成摘要
  - 用 LLM 生成高层摘要，替换原始详细消息

关键设计：
1. **截断 vs 压缩**：截断是丢失信息但快速；压缩是保留语义但消耗 LLM 调用
2. **预算检查时机**：每次用户输入后、调用 LLM 前检查
3. **可配置性**：通过 Agent 构造参数启用/禁用和调节阈值

Agent 新增参数：
- `enable_budget`: 是否启用 tool result 预算控制
- `enable_compact`: 是否启用自动压缩
- `max_tool_result_length`: tool result 最大长度
- `compact_threshold`: 触发压缩的 token 阈值

#### Step 8 — Max Turns 保护 + 错误处理
**对应 Claude Code 源码：** `query.ts` 中 `maxTurns` 检查 + `withRetry.ts`

核心内容：
- **Max Turns 保护**：防止 Tool Use 循环无限执行
  - 默认限制：20 turns
  - 达到限制时返回错误，提示可能是无限循环

- **指数退避重试**：LLM 调用失败时自动重试
  - 默认重试次数：3 次
  - 退避策略：初始 1 秒，每次翻倍，最大 60 秒
  - 带抖动（jitter）避免 thundering herd

- **工具错误优雅处理**：
  - 工具执行异常捕获，返回错误信息而非抛异常
  - 让 LLM 决定如何处理错误

关键设计：
1. **错误是信息**：工具失败告诉 LLM，让 LLM 决定重试或换方案
2. **网络错误可恢复**：超时、连接错误应该重试
3. **逻辑错误不可恢复**：参数错误、权限错误重试无用

Agent 新增参数：
- `enable_retry`: 是否启用 LLM 调用重试
- `max_retries`: LLM 调用最大重试次数

#### Step 9 — 配置系统 + CLI 完善
**对应 Claude Code 源码：** 配置系统

核心内容：
- **统一配置管理**：Config 类统一管理所有配置
- **配置优先级**：命令行参数 > 环境变量 > 默认值
- **环境变量支持**：所有配置项都可通过环境变量设置
- **更完善的 CLI**：更详细的帮助信息，更多配置选项

配置优先级：
1. 命令行参数（最高优先级）
2. 环境变量
3. 默认值

支持的环境变量：
- `LLM_PROVIDER`: 默认 LLM 后端
- `ANTHROPIC_MODEL` / `OPENAI_MODEL`: 模型名称
- `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL`: API 地址
- `AGENT_MAX_TURNS`: 最大 turn 数
- `AGENT_MAX_RETRIES`: 最大重试次数
- `AGENT_NO_BUDGET`: 禁用 budget 控制
- `AGENT_NO_COMPACT`: 禁用自动压缩
- `AGENT_NO_RETRY`: 禁用重试
- `AGENT_NO_STREAM`: 禁用流式输出

CLI 新增参数：
- `--max-turns`: 设置最大 turn 数
- `--no-budget`: 禁用 budget 控制
- `--no-compact`: 禁用自动压缩
- `--no-retry`: 禁用重试

#### Step 10 — 权限系统
**对应 Claude Code 源码：** `utils/permissions.ts`

核心内容：
- **ask 模式**：工具执行前询问用户确认
  - human-in-the-loop 设计
  - 终端交互式确认
  - 支持批量确认（yes to all）

- **allow/deny 规则**：支持规则配置
  - 通配符匹配（如 `bash(git *)`, `read(*.py)`）
  - fail-closed 设计：未明确允许的操作默认拒绝
  - 预设配置：默认模式、宽松模式、严格模式

权限模式：
- `ask`: 默认模式，危险操作会询问用户确认
- `allow`: 宽松模式，自动允许所有操作（除了最危险的）
- `strict`: 严格模式，自动拒绝所有未明确允许的操作

规则检查顺序：
1. 检查 deny 规则 -> 如果匹配，拒绝
2. 检查 allow 规则 -> 如果匹配，允许
3. 使用默认模式

Agent 新增参数：
- `permission_config`: 权限配置
- `enable_permission`: 是否启用权限检查

CLI 新增参数：
- `--permission-mode`: 设置权限模式 (ask/allow/strict)
- `--no-permission`: 完全禁用权限检查

环境变量：
- `AGENT_PERMISSION_MODE`: 权限模式
- `AGENT_NO_PERMISSION`: 禁用权限检查

#### Step 11 — Prompt Caching
**对应 Claude Code 源码：** `services/api/claude.ts` 中的 prompt cache 逻辑

核心内容：
- **Anthropic cache_control 标记**：
  - system prompt 转为 content blocks 格式，最后一个 block 加 `cache_control: {"type": "ephemeral"}`
  - 从最新消息往前搜索最近的 assistant 消息，在其 content 末尾打 cache_control 标记
  - Anthropic 看到 breakpoint 后，将该位置之前的所有内容编译进缓存

- **OpenAI 自动 caching**：
  - gpt-4o / gpt-4o-mini 等模型自动缓存，无需手动标记
  - 从 `usage.prompt_tokens_details.cached_tokens` 读取命中数

- **LLMResponse 新增字段**：
  - `cache_read_tokens`: 本次命中缓存读取的 token 数（Anthropic 折扣 90%，OpenAI 折扣 50%）
  - `cache_write_tokens`: 本次首次写入缓存的 token 数（略贵）

- **缓存分析命令**：
  - CLI: `--show-cache` 参数，单次请求后显示缓存分析
  - REPL: `/cache` 命令，交互模式中随时查看缓存状态
  - 根据后端（Anthropic / OpenAI）显示不同分析信息
  - 显示 system prompt、tools、消息历史的 token 分布和缓存覆盖率

- **流式模式 token 用量**：
  - `StreamEvent` 新增 `usage` 类型事件，在流结束后报告 token 和 cache 用量
  - 流式和非流式模式都能看到缓存命中/写入信息

Prompt Caching 的成本模型：
```
正常输入：$X / 1M tokens
缓存写入：$X × 1.25 / 1M tokens   （首次，略贵）
缓存命中：$X × 0.1  / 1M tokens   （Anthropic，便宜 90%）
缓存命中：$X × 0.5  / 1M tokens   （OpenAI，便宜 50%）
```

适合缓存的内容（长、稳定、重复使用）：
1. **system prompt**：每次请求都相同，是最佳缓存对象
2. **工具定义（tools）**：工具列表不变，也会自动参与缓存
3. **长对话历史**：多轮对话的早期消息几乎不变

关键设计：
1. **breakpoint 位置**：Anthropic 最多 4 个，我们用 2 个（system + 历史消息），留余量给 tools
2. **浅拷贝**：`_add_cache_control_to_messages` 不修改原始消息列表，只修改要发送的副本
3. **content block 格式转换**：纯字符串 content 需要先转为 `[{"type": "text", "text": "..."}]` 格式才能加 cache_control
4. **后端差异化**：`show_cache_info` 根据 `_is_anthropic` 显示不同缓存策略说明

CLI 新增参数：
- `--show-cache`: 请求结束后显示缓存分析

REPL 新增命令：
- `/cache`: 显示当前缓存分析

#### Step 12 — 并发工具执行
**对应 Claude Code 源码：** `services/tools/StreamingToolExecutor.ts` + `services/tools/toolOrchestration.ts`

核心内容：
- **工具并发安全声明**：`Tool.is_concurrency_safe(input) -> bool`
  - 每个工具根据自身特性（和 input 参数）声明是否可以并发执行
  - 默认 `False`（保守策略），只读工具返回 `True`
  - `BashTool` 根据命令内容动态判断（只读命令安全，写命令不安全）

- **分区策略**：`Agent._partition_tool_calls()`
  - 将工具调用列表分为若干批次
  - 连续的并发安全工具归入同一批次（并发执行）
  - 非并发安全工具独占一个批次（串行执行）

- **并发执行**：`Agent._execute_tools()` 使用 `ThreadPoolExecutor`
  - 单个工具的批次直接执行（无需线程池开销）
  - 多个并发安全工具使用线程池并行执行
  - 结果按原始顺序返回，保证 tool_result 顺序正确

- **流式模式**：`stream()` 方法
  - 每个批次先 yield 所有 `tool_use_start` 事件
  - 并发执行后按原始顺序 yield `tool_use_end` 事件

工具并发安全分类：

| 工具 | 安全性 | 原因 |
|---|---|---|
| `get_current_time` | 始终安全 | 纯函数 |
| `read` | 始终安全 | 只读 |
| `glob` | 始终安全 | 只读 |
| `grep` | 始终安全 | 只读 |
| `bash` | 条件判断 | 根据命令是否只读动态决定 |
| `write` | 不安全 | 修改文件 |
| `edit` | 不安全 | 修改文件 |

关键设计：
1. **输入决定安全性**：`is_concurrency_safe(input)` 接收 input 参数，允许同一工具在不同调用中返回不同结果（如 bash）
2. **保守默认值**：默认 `False`，新工具如果忘记实现不会出并发问题
3. **批次作为屏障**：非安全工具作为执行屏障，前一批次全部完成后才执行下一批次
4. **结果顺序不变**：无论并发还是串行，tool_result 消息的顺序始终与 tool_use 一致

### Step 12：并发工具执行

**并发执行（默认启用，无需额外配置）：**

当 LLM 在一次响应中请求多个工具调用时，Agent 会自动判断哪些工具可以并发执行：

```bash
# 例如让 Agent 同时读取多个文件
python -m agent "读取 agent/agent.py 和 agent/tools/base.py 的内容"
```

Agent 会同时调用两次 `read`（并发安全），而不是串行等待。

**工具并发安全分类：**

| 工具 | 并发安全 | 原因 |
|---|---|---|
| `get_current_time` | 始终安全 | 纯函数，无副作用 |
| `read` | 始终安全 | 只读操作 |
| `glob` | 始终安全 | 只读操作 |
| `grep` | 始终安全 | 只读操作 |
| `bash` | **条件判断** | 只读命令安全（ls、cat、git status），写命令不安全 |
| `write` | 不安全 | 修改文件系统 |
| `edit` | 不安全 | 修改文件系统 |

**分区策略示例：**

```python
# LLM 返回 6 个工具调用：
#   read(a.py), glob(*.py), grep(foo), write(b.py), read(c.py), read(d.py)
#
# 安全性: [True, True, True, False, True, True]
# 分区为 3 个批次:
#   批次 1: [read, glob, grep] → 并发执行（3 个线程）
#   批次 2: [write]            → 串行执行（独占）
#   批次 3: [read, read]       → 并发执行（2 个线程）
```

**在 Python 代码中使用：**

```python
from agent.tools.base import Tool

class MyReadOnlyTool(Tool):
    @property
    def name(self) -> str:
        return "my_tool"

    def is_concurrency_safe(self, input: dict) -> bool:
        # 根据 input 动态判断
        return input.get("mode") == "read"

    # ... description, input_schema, call
```

### Step 13：子 Agent（SubAgent）

**使用子 Agent（自动启用）：**

Agent 会自动获得 `agent` 工具，LLM 可以自行决定何时启动子 Agent：

```bash
# LLM 可能会启动子 Agent 来并行搜索多个目录
python -m agent "分析 agent/tools/ 和 agent/llm/ 两个目录的代码结构"
```

**在 Python 代码中使用：**

```python
from agent.llm import create_llm
from agent.agent import Agent
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
agent = Agent(llm=llm, tools=get_tools())

# Agent 自动包含 agent 工具
print([t.name for t in agent.tools])
# ['get_current_time', 'bash', 'read', 'write', 'edit', 'glob', 'grep', 'agent']

# LLM 决定是否使用子 Agent
response = agent.chat("搜索项目中所有 TODO 注释，按模块分类汇总")
# LLM 可能启动多个子 Agent 分别搜索不同目录
```

**直接使用 AgentTool：**

```python
from agent.tools.agent_tool import AgentTool
from agent.tools import get_tools
from agent.llm import create_llm

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')

# 手动创建子 Agent 工具
agent_tool = AgentTool(
    llm=llm,
    tools=get_tools(),
    max_turns=5,
)

result = agent_tool.call({
    "prompt": "读取 agent/agent.py 并列出所有公开方法",
    "description": "分析 Agent API",
})
print(result)
```

### Step 14：Agent 间通信

**结构化结果输出：**

Step 14 增强了子 Agent 的返回格式，包含 `agentId`、`status` 和详细统计信息：

```bash
# Agent 启动子任务后，返回结构化结果
python -m agent "搜索 agent/tools/ 目录下所有 Python 文件并分析功能"
```

返回格式示例：
```
[子 Agent: 搜索工具文件]
agentId: agent-a1b2c3d4
status: completed

agent/tools/ 目录下有 9 个 Python 文件：
- base.py: Tool 抽象基类...
- bash.py: Bash 命令执行工具...

--- 统计: turns=3, tool_uses=5, duration=2340ms, total_input_tokens=1200, total_output_tokens=450 ---
```

**SendMessage 继续对话：**

子 Agent 完成后，主 Agent 可以通过 `send_message` 工具向同一个子 Agent 发送后续消息：

```bash
# LLM 可能先启动子 Agent 做初步分析，然后追问细节
python -m agent "先概述 agent/ 目录的架构，然后深入分析 agent.py 中最复杂的方法"
```

LLM 会自动完成两步：
1. 调用 `agent` 工具：子 Agent 分析目录架构，返回 `agentId`
2. 调用 `send_message` 工具：使用 `agentId` 向同一个子 Agent 追问 `agent.py` 的细节

**自定义子 Agent system prompt：**

```bash
# LLM 可以为子 Agent 指定专门的角色
python -m agent "用严格的代码审查标准检查 agent/agent.py"
```

LLM 可能传递：
```json
{
  "prompt": "审查 agent/agent.py 中的错误处理逻辑",
  "description": "代码审查",
  "system": "你是一个严格的代码审查专家，关注错误处理、边界条件和安全问题。"
}
```

**在 Python 代码中使用：**

```python
from agent.llm import create_llm
from agent.agent import Agent
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
agent = Agent(llm=llm, tools=get_tools())

# Agent 自动包含 agent 和 send_message 工具
print([t.name for t in agent.tools])
# ['get_current_time', 'bash', 'read', 'write', 'edit', 'glob', 'grep', 'agent', 'send_message']

# 子 Agent 注册表（由 agent 和 send_message 共享）
print(agent._sub_agent_registry)  # {}

# 直接使用 AgentTool（结构化输出）
from agent.tools.agent_tool import AgentTool
agent_tool = AgentTool(
    llm=llm,
    tools=get_tools(),
    max_turns=5,
    agent_registry={},
)
result = agent_tool.call({
    "prompt": "列出当前目录结构",
    "description": "目录分析",
})
print(result)  # 包含 agentId、status、统计信息

# 直接使用 SendMessageTool
from agent.tools.send_message import SendMessageTool
registry = agent_tool._agent_registry  # 复用注册表
send_tool = SendMessageTool(agent_registry=registry)
result = send_tool.call({
    "agent_id": "agent-a1b2c3d4",  # 从上面结果中获取
    "message": "只列出 Python 文件",
})
print(result)
```

### Step 15：Coordinator 模式（并发多 Agent）

**启用 Coordinator 模式：**

```bash
# Coordinator 模式：主 Agent 编排子 Agent 执行任务
python -m agent --coordinator "分析 agent/tools/ 和 agent/llm/ 两个目录的代码结构"
```

Coordinator 会同时启动多个 Worker（子 Agent）并行分析两个目录，然后综合结果回复。

**环境变量启用：**

```bash
export AGENT_COORDINATOR=1
python -m agent "重构 config.py 并更新相关测试"
```

**在 Python 代码中使用：**

```python
from agent.llm import create_llm
from agent.agent import Agent
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
agent = Agent(
    llm=llm,
    tools=get_tools(),
    coordinator_mode=True,  # 启用 Coordinator 模式
)

# Coordinator 会自动拆解任务并分配给 Worker
response = agent.chat("搜索项目中所有 TODO 注释，按模块分类汇总")
```

**task-notification 格式：**

Coordinator 模式下，Worker 的返回格式为结构化 XML：

```xml
<task-notification>
<task-id>agent-a1b2c3d4</task-id>
<description>搜索工具文件</description>
<status>completed</status>
<result>
找到 9 个 Python 文件...
</result>
<usage>
  <turns>3</turns>
  <tool_uses>5</tool_uses>
  <duration_ms>2340</duration_ms>
  <total_input_tokens>1200</total_input_tokens>
  <total_output_tokens>450</total_output_tokens>
</usage>
</task-notification>
```

### Step 16：会话持久化（Session Persistence）

**创建新会话（默认自动启用）：**

```bash
# 每次运行自动创建会话，退出后可恢复
python -m agent "分析项目结构"
# 输出中会显示：会话 ID: abc12345
```

**恢复已有会话：**

```bash
# 使用会话 ID 从中断处继续
python -m agent --resume abc12345
# 恢复消息历史后，可以继续对话
```

**列出可恢复的会话：**

```bash
python -m agent --list-sessions
```

输出示例：
```
可用会话（共 3 个）:
  abc12345  2026-04-04 22:30  [12 条消息]  分析项目结构
  def67890  2026-04-04 21:15  [6 条消息]   重构 config.py
  ghi24680  2026-04-04 20:00  [4 条消息]   修复 bug
```

**禁用会话持久化：**

```bash
python -m agent --no-session "一次性问题，不需要保存"
```

**环境变量控制：**

```bash
# 禁用会话持久化
export AGENT_NO_SESSION=1

# 恢复指定会话
export AGENT_RESUME_SESSION=abc12345
python -m agent
```

**在交互模式中查看会话信息：**

```bash
python -m agent
[1] 你: /session
--- 当前会话 ---
  会话 ID: abc12345
  项目: /Users/xxx/project
  模型: claude-sonnet-4-20250514
  消息数: 6
  恢复命令: python -m agent --resume abc12345
```

**在 Python 代码中使用：**

```python
from agent.session import SessionManager

# 创建新会话
sm = SessionManager(project="/path/to/project", model="claude-sonnet-4-20250514")
sm.append_message({"role": "user", "content": "你好"})
sm.append_message({"role": "assistant", "content": [{"type": "text", "text": "你好！"}]})

# 恢复会话
sm2 = SessionManager.resume("abc12345")
messages = sm2.load_messages()
print(f"恢复了 {len(messages)} 条消息")

# 列出会话
sessions = SessionManager.list_sessions(project="/path/to/project")
print(SessionManager.format_session_list(sessions))

# 与 Agent 集成
from agent.agent import Agent
from agent.llm import create_llm
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
agent = Agent(llm=llm, tools=get_tools(), session_manager=sm)
agent.chat("问题")  # 自动持久化到 JSONL 文件
```

### Step 17：Hooks 系统

**配置 Hooks（创建 `~/.coding-agent/settings.json` 或项目目录下 `.coding-agent/settings.json`）：**

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [
          {"type": "command", "command": "echo \"About to run: $TOOL_INPUT\""}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {"type": "command", "command": "echo \"Tool $TOOL_NAME finished\""}
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {"type": "command", "command": "echo \"User said: $USER_PROMPT\""}
        ]
      }
    ]
  }
}
```

**PreToolUse Hook（工具执行前）：**

```bash
# Hook 通过环境变量接收上下文
# HOOK_EVENT=PreToolUse, TOOL_NAME=bash, TOOL_INPUT={"command":"ls"}, CWD=/path

# exit 0 = 允许执行
# exit 2 = 阻止执行（stdout 作为 block reason）
```

**PostToolUse Hook（工具执行后）：**

```bash
# HOOK_EVENT=PostToolUse, TOOL_NAME=bash, TOOL_INPUT=..., TOOL_RESULT=..., CWD=...
# stdout 作为 additional context 附加到工具结果
```

**PreToolUse 阻止工具执行：**

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [
          {"type": "command", "command": "if echo $TOOL_INPUT | grep -q 'rm -rf'; then echo 'Dangerous command blocked' && exit 2; fi"}
        ]
      }
    ]
  }
}
```

**PreToolUse 修改工具输入（JSON 输出）：**

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [
          {"type": "command", "command": "echo '{\"updatedInput\": {\"command\": \"ls -la\"}}'"}
        ]
      }
    ]
  }
}
```

**禁用 Hooks：**

```bash
python -m agent --no-hooks "不执行任何 Hook"
export AGENT_NO_HOOKS=1
```

**在交互模式中查看 Hooks：**

```bash
python -m agent
[1] 你: /hooks
Hooks 配置:
  PreToolUse (1 个):
    bash: ['echo "About to run: $TOOL_INPUT"']
  PostToolUse (1 个):
    *: ['echo "Tool $TOOL_NAME finished"']
```

**在 Python 代码中使用：**

```python
from agent.hooks import HookManager

hm = HookManager(cwd="/path/to/project")

# 工具执行前
result = hm.run_pre_tool_use("bash", {"command": "ls"})
if result.is_blocked:
    print(f"被 Hook 阻止: {result.block_reason}")

# 工具执行后
result = hm.run_post_tool_use("bash", {"command": "ls"}, "file1\nfile2")
for ctx in result.additional_contexts:
    print(f"Hook context: {ctx}")

# 与 Agent 集成
from agent.agent import Agent
from agent.llm import create_llm
from agent.tools import get_tools

llm = create_llm('anthropic', 'claude-sonnet-4-20250514')
agent = Agent(llm=llm, tools=get_tools(), hook_manager=hm)
agent.chat("列出文件")  # 工具调用前后自动执行 Hook
```

---

#### Step 13 — 子 Agent（SubAgent）
**对应 Claude Code 源码：** `tools/AgentTool/`

核心内容：
- **`agent` 工具**：主 Agent 可以启动独立子 Agent 执行子任务
  - 子 Agent 拥有独立的消息历史和 tool use 循环
  - 完成后将结果文本返回给主 Agent 作为 tool_result
  - 并发安全：多个子 Agent 可以同时运行（Step 12 自动并发）

- **自动注入**：Agent 初始化时自动创建并添加 `agent` 工具
  - 子 Agent 继承父 Agent 的 LLM 和工具集
  - 子 Agent 不包含 `agent` 工具（`_enable_agent_tool=False`，防止无限递归）

- **隔离设计**：
  - 独立消息历史（`Agent` 新实例，空 `messages`）
  - 关闭自动压缩（子任务通常较短）
  - `max_turns` 更保守（默认 10，不超过父 Agent）
  - 权限检查由父 Agent 统一管理

关键设计：
1. **不在 BUILTIN_TOOLS 中注册**：`AgentTool` 需要运行时的 LLM 和工具配置，无法在模块加载时创建
2. **延迟导入**：`AgentTool.call()` 中 `from agent.agent import Agent` 避免循环引用
3. **工具集排除自身**：`sub_tools = [t for t in self.tools if t.name != 'agent']`
4. **结果格式**：包含任务描述、结果文本和统计信息（turns、tokens）

#### Step 14 — Agent 间通信
**对应 Claude Code 源码：** `tools/AgentTool/` 的结构化输出 + `SendMessage` 机制

核心内容：
- **结构化输出**：子 Agent 返回包含 `agentId`、`status`、`content`、统计信息的结构化结果
  - `agentId`：唯一标识（`agent-{uuid}`），用于后续 `send_message` 引用
  - `status`：执行状态（`completed`）
  - 统计：`turns`、`tool_uses`、`duration`、`total_input_tokens`、`total_output_tokens`

- **Agent 注册表**：`Agent._sub_agent_registry: dict[str, Agent]`
  - 主 Agent 维护一个 `agent_id → Agent` 的字典
  - 子 Agent 完成后自动注册，后续可通过 `send_message` 继续对话
  - `AgentTool` 和 `SendMessageTool` 共享同一个注册表引用

- **SendMessage 工具**：`send_message(agent_id, message)`
  - 通过 `agent_id` 找到已存在的子 Agent
  - 发送新消息，继续已有的对话上下文
  - 子 Agent 保留完整消息历史，理解后续问题
  - 不可并发安全（同一子 Agent 不应同时收到多条消息）

- **自定义 system prompt**：`agent` 工具新增 `system` 可选参数
  - 每次调用可覆盖默认的子 Agent system prompt
  - 适合特定角色（代码审查、安全分析等）

- **Agent 统计追踪**：`Agent` 新增内部计数器
  - `_total_input_tokens`：累计输入 token 数
  - `_total_output_tokens`：累计输出 token 数
  - `_tool_use_count`：累计工具调用次数
  - 在 `_run_tool_loop()` 和 `stream()` 中每次 LLM 响应后累加

关键设计：
1. **共享注册表**：`AgentTool` 和 `SendMessageTool` 通过引用同一个 `dict` 实现通信
2. **子 Agent 排除通信工具**：`sub_tools` 排除 `agent` 和 `send_message`，防止递归
3. **不可并发的 send_message**：`is_concurrency_safe()` 返回 `False`，避免消息历史冲突
4. **per-call system prompt**：`input.get("system")` 优先于 `__init__` 时的默认值
5. **LLM 作为编排器**：不做编程式任务分解/结果聚合，由 LLM 自行决定何时创建/联系子 Agent

#### Step 15 — Coordinator 模式（并发多 Agent）
**对应 Claude Code 源码：** `coordinator/coordinatorMode.ts`

核心内容：
- **Coordinator 系统提示**：专用 system prompt 指导 LLM 扮演"协调者"
  - 任务拆解为调研 → 综合 → 实施 → 验证四阶段
  - 强调并行启动 Worker、自包含指令、综合而非偷懒委托
  - Worker 使用独立的 `WORKER_SYSTEM_PROMPT`，聚焦执行

- **task-notification 格式**：Worker 结果格式化为 XML
  - `<task-id>`：Worker 的 agentId，可用于 send_message 继续对话
  - `<status>`：completed/failed
  - `<result>`：任务结果文本
  - `<usage>`：turns、tool_uses、duration_ms、token 统计

- **Coordinator 上下文**：`build_coordinator_context()`
  - 向 Coordinator 注入 Worker 可用的工具列表
  - 帮助 Coordinator 理解 Worker 的能力边界

- **新增文件**：`agent/coordinator.py`
  - `COORDINATOR_SYSTEM_PROMPT`：Coordinator 角色提示
  - `WORKER_SYSTEM_PROMPT`：Worker 角色提示
  - `build_coordinator_context()`：构建 Worker 工具上下文
  - `format_task_notification()`：格式化 XML 通知

关键设计：
1. **同一个 LLM，不同系统提示**：Coordinator 和普通 Agent 用同一个模型，行为差异完全由 system prompt 决定
2. **复用并发机制**：多个 `agent` 工具调用通过 Step 12 的 `ThreadPoolExecutor` 并发执行
3. **XML 格式的结构化通知**：方便 Coordinator LLM 解析 Worker 结果
4. **三层 system prompt 优先级**：调用时指定 > Worker 默认 > 普通子 Agent 默认

Agent 新增参数：
- `coordinator_mode`: 是否启用 Coordinator 模式

CLI 新增参数：
- `--coordinator`: 启用 Coordinator 模式

环境变量：
- `AGENT_COORDINATOR`: 启用 Coordinator 模式

#### Step 16 — 会话持久化
**对应 Claude Code 源码：** `history.ts` + `sessionStorage.ts` + `sessionRestore.ts`

核心内容：
- **JSONL 格式存储**：每个会话一个 `.jsonl` 文件
  - 首行为元数据（session_id、project、model、created_at、message_count、first_prompt）
  - 后续每行为一条消息，追加写入（append-only）
  - 消息保留完整格式（role、content），支持 Anthropic 和 OpenAI 两种格式

- **SessionManager 类**：会话持久化管理器
  - `append_message(message)`: 追加单条消息到 JSONL 文件
  - `load_messages()`: 从 JSONL 文件加载消息历史
  - `resume(session_id)`: 从已有会话恢复（类方法）
  - `list_sessions()`: 列出所有可恢复的会话（静态方法）
  - `format_session_list()`: 格式化会话列表为可读文本

- **Agent 集成**：
  - `session_manager` 参数：传入 `SessionManager` 实例启用持久化
  - 每次消息追加到 `self.messages` 后自动调用 `_persist_message()`
  - 三个持久化点：用户消息、assistant 消息、tool_result 消息
  - `restore_messages(messages)`: 恢复消息历史

- **会话存储目录**：`~/.coding-agent/sessions/`
  - 每个会话文件名：`{session_id}.jsonl`
  - session_id 为 UUID 前 8 位（如 `abc12345`）

关键设计：
1. **追加写入**：消息逐条追加到文件，不会丢失已写入的消息
2. **元数据首行**：方便 `list_sessions` 快速扫描，不需要读取整个文件
3. **格式无关**：消息以原始 dict 格式存储，兼容 Anthropic 和 OpenAI
4. **首行重写**：每次追加消息后更新元数据行的 updated_at 和 message_count
5. **子 Agent 不持久化**：`session_manager` 只传递给主 Agent

Agent 新增参数：
- `session_manager`: 会话持久化管理器

CLI 新增参数：
- `--resume SESSION_ID`: 恢复指定会话
- `--list-sessions`: 列出可恢复的会话
- `--no-session`: 禁用会话持久化

REPL 新增命令：
- `/session`: 显示当前会话信息

环境变量：
- `AGENT_NO_SESSION`: 禁用会话持久化
- `AGENT_RESUME_SESSION`: 恢复指定会话 ID

#### Step 17 — Hooks 系统
**对应 Claude Code 源码：** `utils/hooks.ts` + `types/hooks.ts` + `schemas/hooks.ts`

核心内容：
- **三种 Hook 事件**：
  - `PreToolUse`：工具执行前触发，可以阻止执行（exit 2）或修改输入
  - `PostToolUse`：工具执行后触发，stdout 作为 additional context 注入对话
  - `UserPromptSubmit`：用户提交消息后触发，stdout 作为 additional context 注入

- **Hook 配置格式**（`~/.coding-agent/settings.json` 或项目 `.coding-agent/settings.json`）：
  - `hooks` 字段包含按事件名分组的 matcher 数组
  - 每个 matcher 有 `matcher`（工具名通配符匹配）和 `hooks`（命令列表）
  - 项目配置追加到全局配置之后

- **Hook 命令执行**：
  - 通过 `subprocess.run()` 执行 shell 命令
  - 环境变量传递上下文：`TOOL_NAME`、`TOOL_INPUT`、`TOOL_RESULT`、`USER_PROMPT`、`CWD`、`HOOK_EVENT`
  - exit 0 = 成功，exit 2 = 阻止（stdout 作为 block reason），其他 = 非阻塞错误
  - 默认超时 10 秒

- **JSON 输出协议**：
  - stdout 以 `{` 开头时尝试解析为 JSON
  - `updatedInput` 字段：修改工具输入参数
  - `additionalContext` 字段：注入额外上下文

- **HookManager 类**：
  - `run_pre_tool_use(tool_name, tool_input)`: 执行 PreToolUse Hook
  - `run_post_tool_use(tool_name, tool_input, tool_result)`: 执行 PostToolUse Hook
  - `run_user_prompt_submit(prompt)`: 执行 UserPromptSubmit Hook
  - `has_hooks(event)`: 检查是否有配置的 Hook
  - `get_hooks_summary()`: 获取 Hook 配置摘要
  - `reload()`: 重新加载配置

- **Agent 集成**：
  - `hook_manager` 参数：传入 `HookManager` 实例启用 Hooks
  - `_execute_tool()` 中调用 PreToolUse（权限检查后、工具执行前）和 PostToolUse（工具执行后）
  - `_run_hook_user_prompt()` 在 `_run_tool_loop()` 和 `stream()` 开头调用 UserPromptSubmit

关键设计：
1. **exit code 约定**：exit 2 = block，与 Claude Code 一致
2. **环境变量传递**：不需要 stdin/stdout 协议，简单高效
3. **聚合结果**：多个 Hook 按顺序执行，任何一个 block 则整体 block
4. **配置合并**：全局 + 项目两级配置，项目配置追加在后
5. **子 Agent 不继承**：hook_manager 只传递给主 Agent

Agent 新增参数：
- `hook_manager`: Hooks 管理器

CLI 新增参数：
- `--no-hooks`: 禁用 Hooks

REPL 新增命令：
- `/hooks`: 查看当前 Hooks 配置

环境变量：
- `AGENT_NO_HOOKS`: 禁用 Hooks

### 已完成（第二阶段）

> 第一阶段（Step 1-17）已完成 Agent 核心循环；第二阶段聚焦 **项目感知、智能记忆、可扩展性** 三个方向。
> 以下 6 个功能已全部实现。

---

#### Step 18 — 项目记忆文件（AGENTS.md 增强 + 规则目录）
**对应 Claude Code 源码：** `utils/claudemd.ts` + `context.ts`

已实现。重写 `agent/context.py`，支持分层记忆文件加载：

核心内容：
- **MemoryFile 数据类**：path、content、memory_type（User/Project/Local）、source
- **分层加载**（`load_memory_files()`）：
  - 用户级：`~/.coding-agent/AGENTS.md` + `~/.coding-agent/rules/*.md`
  - 项目级（从根目录到 cwd，越近优先级越高）：`AGENTS.md`、`.coding-agent/AGENTS.md`、`.coding-agent/rules/*.md`
  - 本地级：`AGENTS.local.md`（gitignore，个人本地配置）
- **`@include` 指令**：`@./path`、`@~/path`、`@/abs/path`，递归展开（最大深度 5），防循环引用，代码块内不解析
- **截断保护**：总记忆内容上限 40000 字符
- **`build_system_prompt()`**：将所有记忆文件按 `[Type] source` 标签组装到 system prompt
- **向后兼容**：`get_context_info()` 保留 `agents_md_loaded` / `agents_md_path` 字段
- **CLI 显示**：启动时列出所有已加载的记忆文件及类型

---

#### Step 19 — Plan Mode（规划模式）
**对应 Claude Code 源码：** `tools/EnterPlanModeTool/` + `tools/ExitPlanModeTool/` + `utils/planModeV2.ts`

已实现。新增 `agent/tools/plan_mode.py`，在 Agent 中集成规划模式：

核心内容：
- **EnterPlanModeTool**：Agent 调用后进入规划模式，`Agent.plan_mode = True`
- **ExitPlanModeTool**：Agent 提交方案（plan 参数），退出规划模式恢复全部权限
- **只读检查**（`is_tool_readonly()`）：
  - 白名单工具（read, glob, grep, get_current_time, agent, send_message）直接通过
  - bash 工具按命令前缀判断（ls, cat, git status/log/diff 等允许，其他拒绝）
  - write, edit 等写入工具在规划模式下被拒绝
- **Agent 集成**：在 `_execute_tool()` 中，权限检查之后、Hook 之前判断 plan_mode
- **工具自动注册**：主 Agent 初始化时自动添加两个 Plan Mode 工具
- **方案保留**：ExitPlanMode 的方案内容保留在对话上下文中，指导后续执行

---

#### Step 20 — Session Memory（会话记忆）
**对应 Claude Code 源码：** `services/SessionMemory/sessionMemory.ts` + `services/compact/sessionMemoryCompact.ts`

已实现。新增 `agent/session_memory.py`，在 Agent 中集成会话记忆：

核心内容：
- **SessionMemory 数据类**：llm、notes、update_interval、工具调用计数器
- **定期笔记提取**：每 8 次工具调用后，用 LLM 从对话历史中提取/更新结构化笔记
- **笔记模板**（6 个章节）：Session Title、Current State、Task Specification、Key Files、Errors & Corrections、Worklog
- **对话摘要构建**（`_build_conversation_summary()`）：只提取文本内容，工具调用/结果精简显示，单条消息最大 2000 字符，总量上限 30000 字符
- **压缩集成**：上下文压缩后，Session Memory 笔记自动注入 `[历史摘要]` 消息，防止关键信息丢失
- **序列化支持**：`to_dict()` / `from_dict()` 用于会话持久化
- **Agent 集成**：
  - `session_memory` 参数传入 Agent 构造函数
  - `_execute_tool()` 中调用 `record_tool_call()` 计数
  - `_check_and_compact()` 中调用 `maybe_update()` 定期更新笔记
  - 压缩后自动注入笔记到历史摘要
- **`build_agent()` 集成**：自动创建 `SessionMemory` 实例并传入 Agent

---

#### Step 21 — Auto-Memory（跨会话记忆）
**对应 Claude Code 源码：** `memdir/` + `services/extractMemories/`

已实现。新增 `agent/auto_memory.py`，在 Agent 中集成跨会话持久记忆：

核心内容：
- **记忆目录**：`~/.coding-agent/projects/<path-hash>/memory/`，每个项目独立
- **路径 Hash**：SHA256(绝对路径) 前 12 位，确保同一项目映射到同一目录
- **记忆文件**：每条记忆一个 `.md` 文件，带 frontmatter（name, description, type）
- **四种记忆类型**：user（偏好）、feedback（纠正）、project（事实）、reference（引用）
- **MEMORY.md 索引**：自动维护的索引文件，按类型分组列出所有记忆
- **记忆提取**（`extract_and_save()`）：
  - 对话结束时（LLM 最终回复，无工具调用）触发
  - 用 LLM 分析对话历史，提取 JSON 格式的记忆列表
  - 去重检查：已有记忆清单传入提取 prompt
  - 保存文件 + 重建索引
- **记忆加载**（`load_memory_prompt()`）：
  - 新会话启动时读取 MEMORY.md
  - 注入到 system prompt 中，LLM 自动获得历史上下文
- **Agent 集成**：
  - `auto_memory` 参数传入 Agent 构造函数
  - `_maybe_extract_memories()` 在 `_run_tool_loop()` 和 `stream()` 结束时调用
  - `build_agent()` 创建 AutoMemory 并注入记忆到 system prompt
- **REPL 命令**：`/memory` 查看当前项目的所有记忆

---

#### Step 22 — MCP Client（Model Context Protocol）
**对应 Claude Code 源码：** `services/mcp/client.ts` + `services/mcp/config.ts` + `tools/MCPTool/`

已实现。新增 `agent/mcp_client.py`，支持连接 MCP Server 并动态发现工具：

核心内容：
- **MCPServerConfig**：单个 Server 配置（name, command, args, env, transport, url）
- **配置加载**（`load_mcp_config()`）：
  - 全局：`~/.coding-agent/settings.json` 的 `mcpServers` 字段
  - 项目：`.coding-agent/settings.json`，同名覆盖全局
- **MCPConnection**：管理单个 Server 的连接和工具发现
  - 支持 stdio 传输（subprocess + StdioClientTransport）
  - 支持 SSE 传输（HTTP Server-Sent Events）
  - 连接后调用 `tools/list` 发现工具
- **MCPProxyTool**：将 MCP 工具代理为 Agent Tool
  - 名称格式：`mcp_{server}__{tool}` 避免冲突
  - 同步包装异步调用（后台事件循环 + `run_coroutine_threadsafe`）
  - 结果转换：MCP content blocks → 纯文本
- **MCPManager**：管理所有 Server 的连接生命周期
  - `connect_all()` 连接所有配置的 Server，返回工具列表
  - `close_all()` 优雅关闭所有连接
  - `get_status()` 获取连接状态摘要
- **Agent 集成**：
  - `build_agent()` 中连接 MCP Server，MCP 工具追加到内置工具列表
  - 启动时显示已连接的 Server 和工具数量
- **REPL 命令**：`/mcp` 查看 MCP Server 连接状态

---

#### Step 23 — Skills 系统（自定义技能）
**对应 Claude Code 源码：** `skills/loadSkillsDir.ts` + `skills/bundledSkills.ts` + `tools/SkillTool/`

已实现。新增 `agent/skills.py`，支持自定义 Skill 加载和执行：

核心内容：
- **SkillDefinition 数据类**：name、description、prompt、source、allowed_tools、context（fork/inline）
- **Frontmatter 解析**（`_parse_frontmatter()`）：解析 markdown 文件的 YAML-like frontmatter，支持列表格式 `[a, b, c]`
- **Skill 加载**（`load_skills()`）：
  - 用户级：`~/.coding-agent/skills/*/SKILL.md`
  - 项目级：`.coding-agent/skills/*/SKILL.md`（同名覆盖用户级）
- **SkillTool**：Agent 通过工具调用执行 Skill
  - 名称固定为 `skill`，输入参数包含 `name`（Skill 名称）和 `args`（可选参数）
  - 执行方式：fork 子 Agent，使用 Skill 的 prompt 作为系统提示
  - 工具过滤：根据 `allowed_tools` 限制子 Agent 可用工具；未指定则排除递归工具
  - 返回格式化结果，包含 Skill 名称和执行统计
- **SkillManager**：管理 Skill 生命周期
  - `has_skills()`：检查是否有可用 Skill
  - `create_skill_tool()`：创建 SkillTool 实例
  - `get_summary()`：获取 Skill 摘要（名称、描述、工具、来源）
  - `list_skill_names()`：列出所有 Skill 名称
- **Agent 集成**：
  - `build_agent()` 中加载 SkillManager，有 Skill 时创建 SkillTool 追加到工具列表
  - 启动时显示已加载的 Skill 名称
  - `agent._skill_manager` 存储引用
- **REPL 命令**：`/skills` 查看所有已加载的 Skill 详情

关键设计：
1. **Markdown + Frontmatter 格式**：Skill 定义简单直观，frontmatter 声明元数据，正文即 prompt
2. **两级目录**：用户级 + 项目级，项目级同名覆盖用户级
3. **工具过滤**：`allowed_tools` 限制子 Agent 能力边界，防止 Skill 越权
4. **fork 隔离**：子 Agent 独立消息历史，不污染主对话上下文
5. **排除递归**：子 Agent 排除 `skill`、`agent`、`send_message` 等工具，防止无限递归

---

#### 第二阶段实现路线

```
Step 18  项目记忆文件增强     [中]  ← 基础设施，所有记忆功能的入口
Step 19  Plan Mode           [中]  ← 独立功能，无前置依赖
Step 20  Session Memory      [中]  ← 依赖 Step 16 会话持久化
Step 21  Auto-Memory         [高]  ← 依赖 Step 18 项目记忆文件体系
Step 22  MCP Client          [高]  ← 独立功能，Skills 的前置依赖
Step 23  Skills 系统          [高]  ← 依赖 Step 22 MCP
```

排序逻辑：
1. **Step 18 项目记忆文件** 排最前——复杂度中等，改动集中在 system prompt 组装，是后续 Auto-Memory 写入记忆文件的基础
2. **Step 19 Plan Mode** 紧跟——独立功能无前置依赖，对复杂任务质量提升明显
3. **Step 20-21 Session Memory → Auto-Memory** 按依赖顺序——先做会话内记忆，再做跨会话记忆
4. **Step 22-23 MCP → Skills** 放最后——复杂度最高，且 Skills 依赖 MCP 提供外部 Skill 来源

---

## 未来规划

以下功能尚未实现，欢迎社区贡献：

### TodoWriteTool（任务清单）

**对应 Claude Code 源码：** `tools/TodoWriteTool/`

**功能描述：**
- 将 Plan Mode 的方案分解为结构化的任务清单（todo list）
- 每个任务有状态：`pending` / `in_progress` / `completed`
- Agent 执行时更新任务状态，用户可实时看到进度
- 支持增删改查任务，动态调整计划

**与 Plan Mode 的关系：**
- Plan Mode 负责"规划阶段"（只读分析、制定方案）
- TodoWriteTool 负责"执行阶段"（将方案拆解为可跟踪的任务）
- 当前实现：Plan Mode 的方案作为文本保留在对话中，Agent 自行管理执行顺序
- 完整实现：exit_plan_mode 后，Agent 调用 TodoWriteTool 创建任务清单，逐步执行

**实现要点：**
- `TodoWriteTool`：更新整个任务列表（覆盖写）
- `TaskListTool` / `TaskUpdateTool`：查询和更新单个任务（可选）
- 任务状态存储在 Agent 实例中（或持久化到会话）
- CLI 显示当前任务列表（类似 `/memory` 命令）
