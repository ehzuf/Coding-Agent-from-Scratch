# 从零实现 Coding Agent：完整指南


[Claude Code](https://claude.com/product/claude-code) 拥有超过 512,000 行 TypeScript 代码、近 1,900 个源文件。本系列将以它为参考，深入解析每个设计决策背后的核心理念与思考过程。本系列文章适合想了解 Coding Agent 内部实现原理的同学，每篇文章都会配套可落地的实现讲解（包含关键代码与完整串联思路）。

## 这个系列讲什么

简单来说：**从一个最简单的 LLM API 调用开始，一步步构建出一个完整的 Coding Agent。**

整体思路是：

```
先跑起来 → 做到好用 → 支持协作 → 变得聪明
```

每篇解决一个问题，前后篇之间是递进关系。下面按四个板块介绍：

---

## 一、基础能力

> 从 Hello World 到能干活的 Agent

| 序号 | 标题 | 讲了什么 |
|---|---|---|
| 1 | [项目骨架与 LLM 抽象层](01-llm-abstraction.md) | 搭建项目骨架，封装 Anthropic / OpenAI 双后端 |
| 2 | [流式输出](02-streaming-output.md) | 让 Agent 逐字输出，提升交互体验 |
| 3 | [消息历史与多轮对话](03-multi-turn-conversation.md) | 维护消息历史，能连续聊天了 |
| 4 | [Tool Use 协议](04-tool-use-protocol.md) | Agent 学会调用工具——从"只能说"到"能做事" |
| 5 | [文件操作工具组](05-file-tools.md) | 读写文件、搜索代码，有了基本的编程能力 |
| 6 | [System Prompt + AGENTS.md](06-system-prompt-agents-md.md) | 注入身份和项目上下文，Agent 有了"角色意识" |

读完这 6 篇，你就有一个能对话、能读写文件、能理解项目的基础 Agent 了。

---

## 二、工程化

> 从能用到好用

| 序号 | 标题 | 讲了什么 |
|---|---|---|
| 7 | [上下文预算管理](07-context-budget.md) | 对话太长了怎么办？自动压缩，防 token 溢出 |
| 8 | [Max Turns 保护 + 错误处理](08-error-handling.md) | 防止 Agent 陷入死循环，LLM 异常自动重试 |
| 9 | [配置系统 + CLI 完善](09-config-system.md) | 支持多 LLM 后端切换，命令行参数配置 |
| 10 | [权限系统](10-permission-system.md) | 危险操作先问人，别让 Agent 乱来 |
| 11 | [Prompt Caching](11-prompt-caching.md) | 缓存 system prompt，API 费用省 90% |
| 12 | [并发工具执行](12-concurrent-tool-execution.md) | 多个工具并行跑，效率翻倍 |
| 25 | [后台 Bash 命令](25-background-bash.md) | 长任务扔到后台跑，不再卡住对话 |
| 26 | [沙箱与安全隔离](26-sandbox-security.md) | OS 层强制限制文件/网络访问，防止间接攻击 |

这一组解决的是生产环境里绕不开的问题：健壮性、安全性、成本和性能。

---

## 三、架构进阶

> 从单兵到团队

| 序号 | 标题 | 讲了什么 |
|---|---|---|
| 13 | [子 Agent](13-sub-agent.md) | fork 出独立子 Agent 处理子任务 |
| 14 | [Agent 间通信](14-agent-communication.md) | 子 Agent 能给用户发消息了 |
| 15 | [Coordinator 模式](15-concurrent-multi-agent.md) | 多个 Agent 并发协作，分治复杂任务 |
| 16 | [会话持久化](16-session-persistence.md) | 聊到一半断了？恢复回来继续 |
| 17 | [Hooks 系统](17-hooks-system.md) | 生命周期钩子，审计日志、消息通知，按需挂载 |

到这里，Agent 已经能拆解复杂任务、多路并发、中断恢复了。

---

## 四、智能增强

> 从工具到伙伴

| 序号 | 标题 | 讲了什么 |
|---|---|---|
| 18 | [项目记忆文件](18-project-memory-files.md) | 加载分层记忆文件，Agent 能感知项目规范 |
| 19 | [Plan Mode](19-plan-mode.md) | 先想清楚再动手，只读规划模式 |
| 20 | [会话记忆](20-session-memory.md) | 自动总结对话里的关键信息 |
| 21 | [跨会话记忆](21-auto-memory.md) | 记住你的偏好，下次还能用 |
| 22 | [MCP Client](22-mcp-client.md) | 接入 MCP 协议，连接外部工具生态 |
| 23 | [Skills 系统](23-skills.md) | 写个 Markdown 就能定义可复用的工作流 |
| 24 | [Skill 自进化](24-skill-evolution.md) | 复杂任务后自动沉淀经验，越用越聪明 |
| 27 | [可观测性与 Tracing](27-observability-tracing.md) | 成本追踪、Span 计时、事件打点，行为可回溯 |
| 28 | [Agent 评测](28-agent-evaluation.md) | 批量执行、自动评分、轨迹压缩，数字说话 |

到这里，Agent 具备了项目感知、长期记忆、自定义扩展、自我进化、可观测和可评测能力——一个完整的 Coding Agent。

---

## 最后

跟随本系列文章的完整实现路径，你将：

- **系统掌握** Coding Agent 的核心架构与实现原理
- **亲手构建** 一个从基础到智能的完整 Agent 系统
- **深入理解** 每个技术决策背后的权衡与思考
- **获得可复用** 的工程实践与代码范例

