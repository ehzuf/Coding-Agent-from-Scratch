# Coding Agent from Scratch

> **512,000 lines of TypeScript → 7,500 lines of Python. Two tutorial series — demystifying every core mechanism of a Coding Agent, then advancing to a personal assistant.**

[中文](README.md)

[Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview) ships with 512,000+ lines of TypeScript across ~1,900 source files. This project re-implements its core mechanisms in **~7,500 lines of Python** (less than 1.5% of the original codebase), with two tutorial series explaining the *why* behind every design decision.

## Why This Project

There's no shortage of Coding Agent products, but their internals remain opaque. This project cracks them open:

- How does the **Tool Use loop** actually work?
- When the **context window** fills up, how does the Agent decide what to discard?
- When **multiple sub-Agents** run in parallel, how is message history isolated?
- What steps does the **MCP protocol** go through from connection to invocation?

One article per mechanism, with complete runnable code.

### By the Numbers

| | Claude Code Original | This Project |
|---|---|---|
| Language | TypeScript | Python |
| Lines of Code | 512,000+ | **~7,500** |
| Source Files | ~1,900 | **~30** |
| Foundation Tutorials | — | **24 articles** |
| Advanced Tutorials | — | **9 articles** (referencing [Hermes Agent](https://github.com/nousresearch/hermes-agent)) |

## Architecture

```
User Input
  ↓
Agent (Message History + Tool Use Loop)
  ├── LLM Abstraction (Anthropic / OpenAI)
  ├── Tools (bash, read, write, edit, glob, grep)
  ├── Sub-Agent / Coordinator (Parallel Task Orchestration)
  ├── Permission System (ask / allow / strict)
  ├── Context Management (budget + auto-compact)
  ├── Memory System (session memory + auto-memory)
  ├── MCP Client (External Tool Servers)
  └── Skills (Custom Workflows)
```

## Tutorials

### Phase 1: Core Loop

From a script that calls an API, to a fully functional Coding Agent core.

| # | Topic | Tutorial |
|---|-------|----------|
| 01 | LLM Abstraction — Unified Anthropic / OpenAI Interface | [01-llm-abstraction.md](TUTORIAL/01-llm-abstraction.md) |
| 02 | Streaming Output — Typewriter Effect | [02-streaming-output.md](TUTORIAL/02-streaming-output.md) |
| 03 | Multi-turn Conversation — Message History | [03-multi-turn-conversation.md](TUTORIAL/03-multi-turn-conversation.md) |
| 04 | Tool Use Protocol — The Agent's Core Capability | [04-tool-use-protocol.md](TUTORIAL/04-tool-use-protocol.md) |
| 05 | File Tools — bash, read, write, edit, glob, grep | [05-file-tools.md](TUTORIAL/05-file-tools.md) |
| 06 | System Prompt + AGENTS.md — Project Awareness | [06-system-prompt-agents-md.md](TUTORIAL/06-system-prompt-agents-md.md) |
| 07 | Context Budget — Tool Result Budget + Auto Compact | [07-context-budget.md](TUTORIAL/07-context-budget.md) |
| 08 | Error Handling — Max Turns Guard + Exponential Backoff | [08-error-handling.md](TUTORIAL/08-error-handling.md) |
| 09 | Config System — CLI Args > Env Vars > Defaults | [09-config-system.md](TUTORIAL/09-config-system.md) |
| 10 | Permission System — ask / allow / strict Modes | [10-permission-system.md](TUTORIAL/10-permission-system.md) |
| 11 | Prompt Caching — Save 90% Cost with Cache Hits | [11-prompt-caching.md](TUTORIAL/11-prompt-caching.md) |
| 12 | Concurrent Tool Execution — Auto Partitioning + ThreadPoolExecutor | [12-concurrent-tool-execution.md](TUTORIAL/12-concurrent-tool-execution.md) |
| 13 | Sub-Agent — Independent Message History + Isolated Execution | [13-sub-agent.md](TUTORIAL/13-sub-agent.md) |
| 14 | Agent Communication — Registry + SendMessage | [14-agent-communication.md](TUTORIAL/14-agent-communication.md) |
| 15 | Coordinator Pattern — Concurrent Multi-Agent Orchestration | [15-concurrent-multi-agent.md](TUTORIAL/15-concurrent-multi-agent.md) |
| 16 | Session Persistence — JSONL Storage + Resume | [16-session-persistence.md](TUTORIAL/16-session-persistence.md) |
| 17 | Hooks System — Inject Custom Logic Around Tool Calls | [17-hooks-system.md](TUTORIAL/17-hooks-system.md) |

### Phase 2: Intelligence

From "it works" to "it works well" — project awareness, memory, and extensibility.

| # | Topic | Tutorial |
|---|-------|----------|
| 18 | Project Memory Files — Layered AGENTS.md + Rules Dir + @include | [18-project-memory-files.md](TUTORIAL/18-project-memory-files.md) |
| 19 | Plan Mode — Analyze Before Acting, Read-only Planning | [19-plan-mode.md](TUTORIAL/19-plan-mode.md) |
| 20 | Session Memory — In-session Structured Note Extraction | [20-session-memory.md](TUTORIAL/20-session-memory.md) |
| 21 | Auto-Memory — Cross-session Persistent Memory | [21-auto-memory.md](TUTORIAL/21-auto-memory.md) |
| 22 | MCP Client — Connect to External Tool Servers | [22-mcp-client.md](TUTORIAL/22-mcp-client.md) |
| 23 | Skills System — Markdown-defined Reusable Workflows | [23-skills.md](TUTORIAL/23-skills.md) |
| 24 | Skill Evolution — Auto-extract Experience After Complex Tasks | [24-skill-evolution.md](TUTORIAL/24-skill-evolution.md) |

### Advanced Series: From Coding Agent to Personal Assistant

Referencing [Hermes Agent](https://github.com/nousresearch/hermes-agent), dissecting the key capability upgrades from a coding assistant to an intelligent personal assistant.

| # | Topic | Tutorial |
|---|-------|----------|
| 01 | Context References — @file/@diff/@url Syntax | [01-context-references.md](TUTORIAL-ADVANCED/01-context-references.md) |
| 02 | Smart Model Routing — Auto-downgrade Simple Queries | [02-smart-routing.md](TUTORIAL-ADVANCED/02-smart-routing.md) |
| 03 | Cost Tracking & Usage Insights — Token Normalization | [03-usage-insights.md](TUTORIAL-ADVANCED/03-usage-insights.md) |
| 04 | Data Redaction — 40+ Sensitive Pattern Detection | [04-redaction.md](TUTORIAL-ADVANCED/04-redaction.md) |
| 05 | Skill Auto-Generation — Background Agent Periodic Extraction | [05-skill-auto-generation.md](TUTORIAL-ADVANCED/05-skill-auto-generation.md) |
| 06 | SQLite + Full-Text Search — WAL Concurrency + FTS5 | [06-sqlite-fts.md](TUTORIAL-ADVANCED/06-sqlite-fts.md) |
| 07 | Multi-Platform Gateway — Telegram/Discord/Slack Adapters | [07-gateway.md](TUTORIAL-ADVANCED/07-gateway.md) |
| 08 | Cron System — Built-in Task Scheduler | [08-cron-system.md](TUTORIAL-ADVANCED/08-cron-system.md) |
| 09 | RL Training Loop — Trajectory Generation + GRPO Orchestration | [09-rl-training.md](TUTORIAL-ADVANCED/09-rl-training.md) |

## Project Structure

```
agent/
├── __main__.py        # CLI Entry + REPL
├── agent.py           # Agent Core (Message History + Tool Use Loop)
├── coordinator.py     # Coordinator Pattern
├── context.py         # Context Management (Layered Memory Files + System Prompt Assembly)
├── config.py          # Config System
├── session.py         # Session Persistence
├── session_memory.py  # Session Memory
├── auto_memory.py     # Cross-session Memory
├── mcp_client.py      # MCP Client
├── skills.py          # Skills System
├── hooks.py           # Hooks System
├── permission.py      # Permission System
├── llm/
│   ├── base.py            # BaseLLM Abstract Base Class
│   ├── anthropic_llm.py   # Anthropic Backend
│   └── openai_llm.py      # OpenAI Backend
└── tools/
    ├── base.py            # Tool Abstract Base Class
    ├── bash.py            # Shell Command Execution
    ├── read.py / write.py / edit.py  # File Operations
    ├── glob.py / grep.py  # Search Tools
    ├── agent_tool.py      # Sub-Agent Tool
    ├── send_message.py    # Agent Communication
    └── plan_mode.py       # Plan Mode Tool
```

## Quick Start

```bash
# Clone
git clone https://github.com/ehzuf/coding-agent-from-scratch.git
cd coding-agent-from-scratch

# Virtual environment
python -m venv .venv
source .venv/bin/activate

# Install
pip install -e .

# Set API Key (pick one)
export ANTHROPIC_API_KEY=sk-xxx
# or
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://your-compatible-endpoint

# Run
python -m agent "Explain Python in three sentences"

# Interactive mode
python -m agent
```

## Reading Guide

**Core principles:** Start from [01](TUTORIAL/01-llm-abstraction.md) through [05](TUTORIAL/05-file-tools.md). These 5 tutorials cover LLM calls → streaming → multi-turn → Tool Use → file tools — everything for a minimal working Agent.

**Context management and cost control:** Jump to [07 Context Budget](TUTORIAL/07-context-budget.md) and [11 Prompt Caching](TUTORIAL/11-prompt-caching.md).

**Multi-Agent collaboration:** Read [13](TUTORIAL/13-sub-agent.md) → [14](TUTORIAL/14-agent-communication.md) → [15](TUTORIAL/15-concurrent-multi-agent.md).

**Memory system:** Read [20 Session Memory](TUTORIAL/20-session-memory.md) → [21 Auto-Memory](TUTORIAL/21-auto-memory.md).

**MCP and Skills:** Read [22](TUTORIAL/22-mcp-client.md) and [23](TUTORIAL/23-skills.md).

**Advanced series:** See [TUTORIAL-ADVANCED](TUTORIAL-ADVANCED/00-overview.md), covering smart routing, cost tracking, data redaction, skill auto-generation, multi-platform gateway, RL training, and more.

## Tech Stack

- **Python 3.12+**
- **Anthropic SDK** / **OpenAI SDK** — LLM backends
- **MCP SDK** (`mcp>=1.0`) — Model Context Protocol client
- No other external dependencies — all core features built from scratch

## License

MIT

---

> Built by [@ehzuf](https://github.com/ehzuf) as a learning resource for the AI engineering community.
>
> If you find this useful, a star would be appreciated.
