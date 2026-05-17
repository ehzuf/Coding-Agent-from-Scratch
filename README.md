# Coding Agent from Scratch

> **512,000 行 TypeScript → 7,500 行 Python。两套教程系列，从零拆解 Coding Agent 的每一个核心机制，再进阶到个人助理。**

[English](README_EN.md)

[Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview) 有 512,000+ 行 TypeScript、近 1,900 个源文件。这个项目用 **~7,500 行 Python**（不到原版 1.5% 的代码量）重新实现了它的核心功能，并用两套教程逐步拆解每一个设计决策背后的 **为什么**。

## 为什么做这个项目

市面上不缺 "Coding Agent" 产品，但它们的核心实现大多是黑盒。这个项目把黑盒拆开：

- **Tool Use 循环**到底是怎么运转的？
- **上下文窗口**快满了，Agent 怎么决定丢弃什么？
- **多个子 Agent** 并行执行时，消息历史怎么隔离？
- **MCP 协议**从连接到调用，到底经过哪些步骤？

每篇文章解决一个问题，配套完整可运行的代码。

### 数字对比

| | Claude Code 原版 | 本项目 |
|---|---|---|
| 语言 | TypeScript | Python |
| 代码行数 | 512,000+ | **~7,500** |
| 源文件数 | ~1,900 | **~30** |
| 基础教程 | — | **28 篇** |
| 进阶教程 | — | **9 篇**（以 [Hermes Agent](https://github.com/nousresearch/hermes-agent) 为参考） |

## 整体架构

```
用户输入
  ↓
Agent (消息历史 + Tool Use 循环)
  ├── LLM 抽象层 (Anthropic / OpenAI)
  ├── 工具集 (bash, read, write, edit, glob, grep)
  ├── 子 Agent / Coordinator (并行任务编排)
  ├── 权限系统 (ask / allow / strict)
  ├── 上下文管理 (budget + auto-compact)
  ├── 记忆系统 (session memory + auto-memory)
  ├── MCP Client (外部工具服务器)
  └── Skills (自定义工作流)
```

## 教程目录

### 第一阶段：核心循环

从一个能调 API 的脚本，到一个完整的 Coding Agent 核心。

| # | 主题 | 教程 |
|---|------|------|
| 01 | LLM 抽象层 — 统一 Anthropic / OpenAI 接口 | [01-llm-abstraction.md](TUTORIAL/01-llm-abstraction.md) |
| 02 | 流式输出 — 打字机效果的实现 | [02-streaming-output.md](TUTORIAL/02-streaming-output.md) |
| 03 | 多轮对话 — 消息历史管理 | [03-multi-turn-conversation.md](TUTORIAL/03-multi-turn-conversation.md) |
| 04 | Tool Use 协议 — Agent 的核心能力 | [04-tool-use-protocol.md](TUTORIAL/04-tool-use-protocol.md) |
| 05 | 文件操作工具 — bash, read, write, edit, glob, grep | [05-file-tools.md](TUTORIAL/05-file-tools.md) |
| 06 | System Prompt + AGENTS.md — 项目感知 | [06-system-prompt-agents-md.md](TUTORIAL/06-system-prompt-agents-md.md) |
| 07 | 上下文预算 — Tool Result Budget + Auto Compact | [07-context-budget.md](TUTORIAL/07-context-budget.md) |
| 08 | 错误处理 — Max Turns 保护 + 指数退避重试 | [08-error-handling.md](TUTORIAL/08-error-handling.md) |
| 09 | 配置系统 — CLI 参数 > 环境变量 > 默认值 | [09-config-system.md](TUTORIAL/09-config-system.md) |
| 10 | 权限系统 — ask / allow / strict 三种模式 | [10-permission-system.md](TUTORIAL/10-permission-system.md) |
| 11 | Prompt Caching — 缓存命中省 90% 费用 | [11-prompt-caching.md](TUTORIAL/11-prompt-caching.md) |
| 12 | 并发工具执行 — 自动分区 + ThreadPoolExecutor | [12-concurrent-tool-execution.md](TUTORIAL/12-concurrent-tool-execution.md) |
| 13 | 子 Agent — 独立消息历史 + 隔离执行 | [13-sub-agent.md](TUTORIAL/13-sub-agent.md) |
| 14 | Agent 间通信 — 注册表 + SendMessage | [14-agent-communication.md](TUTORIAL/14-agent-communication.md) |
| 15 | Coordinator 模式 — 并发多 Agent 编排 | [15-concurrent-multi-agent.md](TUTORIAL/15-concurrent-multi-agent.md) |
| 16 | 会话持久化 — JSONL 存储 + resume 恢复 | [16-session-persistence.md](TUTORIAL/16-session-persistence.md) |
| 17 | Hooks 系统 — 工具调用前后注入自定义逻辑 | [17-hooks-system.md](TUTORIAL/17-hooks-system.md) |

### 第二阶段：项目感知 + 记忆 + 可扩展

从"能用"到"好用"，让 Agent 理解项目、记住上下文、连接外部世界。

| # | 主题 | 教程 |
|---|------|------|
| 18 | 项目记忆文件 — 分层 AGENTS.md + rules 目录 + @include | [18-project-memory-files.md](TUTORIAL/18-project-memory-files.md) |
| 19 | Plan Mode — 先分析再动手，只读规划模式 | [19-plan-mode.md](TUTORIAL/19-plan-mode.md) |
| 20 | Session Memory — 会话内定期提取结构化笔记 | [20-session-memory.md](TUTORIAL/20-session-memory.md) |
| 21 | Auto-Memory — 跨会话持久记忆 | [21-auto-memory.md](TUTORIAL/21-auto-memory.md) |
| 22 | MCP Client — 连接外部工具服务器 | [22-mcp-client.md](TUTORIAL/22-mcp-client.md) |
| 23 | Skills 系统 — Markdown 定义可复用工作流 | [23-skills.md](TUTORIAL/23-skills.md) |
| 24 | Skill 自进化 — 复杂任务后自动沉淀经验 | [24-skill-evolution.md](TUTORIAL/24-skill-evolution.md) |
| 25 | 后台 Bash 命令 — 长任务扔到后台跑，不再卡住对话 | [25-background-bash.md](TUTORIAL/25-background-bash.md) |
| 26 | 沙箱与安全隔离 — OS 层强制限制文件/网络访问 | [26-sandbox-security.md](TUTORIAL/26-sandbox-security.md) |
| 27 | 可观测性与 Tracing — 成本追踪 + Span 计时 + 事件打点 | [27-observability-tracing.md](TUTORIAL/27-observability-tracing.md) |
| 28 | Agent 评测 — 批量执行 + 自动评分 + 轨迹压缩 | [28-agent-evaluation.md](TUTORIAL/28-agent-evaluation.md) |

### 进阶系列：从 Coding Agent 到个人助理

以 [Hermes Agent](https://github.com/nousresearch/hermes-agent) 为参考，拆解从编程助手到智能个人助理的关键能力升级。

| # | 主题 | 教程 |
|---|------|------|
| 01 | 上下文引用系统 — @file/@diff/@url 语法 | [01-context-references.md](TUTORIAL-ADVANCED/01-context-references.md) |
| 02 | 智能模型路由 — 简单问题自动降级 | [02-smart-routing.md](TUTORIAL-ADVANCED/02-smart-routing.md) |
| 03 | 成本追踪与使用洞察 — token 归一化 + 精确计算 | [03-usage-insights.md](TUTORIAL-ADVANCED/03-usage-insights.md) |
| 04 | 信息脱敏 — 40+ 敏感模式自动识别 | [04-redaction.md](TUTORIAL-ADVANCED/04-redaction.md) |
| 05 | 技能自生成 — 后台审查 Agent 周期性提取 Skill | [05-skill-auto-generation.md](TUTORIAL-ADVANCED/05-skill-auto-generation.md) |
| 06 | SQLite + 全文搜索 — WAL 并发 + FTS5 | [06-sqlite-fts.md](TUTORIAL-ADVANCED/06-sqlite-fts.md) |
| 07 | 多平台消息网关 — Telegram/Discord/Slack 适配 | [07-gateway.md](TUTORIAL-ADVANCED/07-gateway.md) |
| 08 | 定时任务系统 — 内置 Cron 调度器 | [08-cron-system.md](TUTORIAL-ADVANCED/08-cron-system.md) |
| 09 | RL 训练闭环 — 轨迹生成 + GRPO 训练编排 | [09-rl-training.md](TUTORIAL-ADVANCED/09-rl-training.md) |

## 项目结构

```
agent/
├── __main__.py        # CLI 入口 + REPL
├── agent.py           # Agent 核心（消息历史 + Tool Use 循环）
├── coordinator.py     # Coordinator 模式
├── context.py         # 上下文管理（分层记忆文件 + system prompt 组装）
├── config.py          # 配置系统
├── session.py         # 会话持久化
├── session_memory.py  # 会话记忆
├── auto_memory.py     # 跨会话记忆
├── mcp_client.py      # MCP 客户端
├── skills.py          # Skills 系统
├── hooks.py           # Hooks 系统
├── permission.py      # 权限系统
├── llm/
│   ├── base.py            # BaseLLM 抽象基类
│   ├── anthropic_llm.py   # Anthropic 后端
│   └── openai_llm.py      # OpenAI 后端
└── tools/
    ├── base.py            # Tool 抽象基类
    ├── bash.py            # Shell 命令执行
    ├── read.py / write.py / edit.py  # 文件操作
    ├── glob.py / grep.py  # 搜索工具
    ├── agent_tool.py      # 子 Agent 工具
    ├── send_message.py    # Agent 间通信
    ├── plan_mode.py       # 规划模式工具
    └── background/        # 后台 Bash 任务（进程托管 + 通知队列 + SleepTool）
```

## 快速开始

```bash
# 克隆项目
git clone https://github.com/ehzuf/coding-agent-from-scratch.git
cd coding-agent-from-scratch

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -e .

# 设置 API Key（任选其一）
export ANTHROPIC_API_KEY=sk-xxx
# 或
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://your-compatible-endpoint

# 运行
python -m agent "用三句话介绍 Python"

# 交互模式
python -m agent
```

## 阅读建议

**理解核心原理：** 从 [01](TUTORIAL/01-llm-abstraction.md) 开始，按顺序读到 [05](TUTORIAL/05-file-tools.md)。这 5 篇覆盖了 LLM 调用 → 流式输出 → 多轮对话 → Tool Use → 文件操作，是一个最小可用 Agent 的全部。

**上下文管理和成本控制：** 直接看 [07 上下文预算](TUTORIAL/07-context-budget.md) 和 [11 Prompt Caching](TUTORIAL/11-prompt-caching.md)。

**多 Agent 协作：** 看 [13](TUTORIAL/13-sub-agent.md) → [14](TUTORIAL/14-agent-communication.md) → [15](TUTORIAL/15-concurrent-multi-agent.md) 这三篇。

**记忆系统：** 看 [20 Session Memory](TUTORIAL/20-session-memory.md) → [21 Auto-Memory](TUTORIAL/21-auto-memory.md)。

**MCP 和 Skills：** 看 [22](TUTORIAL/22-mcp-client.md) 和 [23](TUTORIAL/23-skills.md)。

**进阶系列：** 看 [TUTORIAL-ADVANCED](TUTORIAL-ADVANCED/00-overview.md)，涵盖智能路由、成本追踪、信息脱敏、技能自生成、多平台网关、RL 训练等。

## 技术栈

- **Python 3.12+**
- **Anthropic SDK** / **OpenAI SDK** — LLM 后端
- **MCP SDK** (`mcp>=1.0`) — Model Context Protocol 客户端
- 无其他外部依赖，所有核心功能从零实现

## License

MIT

---

> Built by [@ehzuf](https://github.com/ehzuf) as a learning resource for the AI engineering community.
>
> If you find this useful, a star would be appreciated.
