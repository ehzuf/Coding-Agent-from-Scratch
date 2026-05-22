# 从零实现 Coding Agent（二十七）：可观测性与 Tracing

Agent 跑了一圈回来告诉你"任务完成"，你可能想知道：

- 这一轮花了多少钱？
- 哪个工具调用最慢？
- 为什么它选了这个方案而不是那个？
- 重试了几次？中间有没有报错？

传统软件有日志、有 APM、有链路追踪。Agent 也需要类似的可观测性——但维度不同：它的核心单位不是 HTTP 请求，而是 **LLM 调用**和**工具执行**。

这一篇先建立 Agent 可观测性的通用模型，然后以 **OpenTelemetry GenAI 语义规范**（业界正在收敛的标准）为基础讲清楚"在哪埋点、记录什么"，最后对比 Claude Code 的私有实现。

---

## Agent 可观测性的核心模型

传统 Web 应用的 tracing 很成熟：一个 HTTP 请求进来，经过网关 → 服务 A → 数据库 → 服务 B，每一跳是一个 span，串起来就是一条 trace。

Agent 的执行结构不同，但可以用同样的 trace/span 模型来描述：

```
Trace（一次会话 session）
  └── Span: invoke_agent "main-agent"           ← 一轮对话
        ├── Span: chat claude-sonnet             ← LLM 推理
        ├── Span: execute_tool bash              ← 工具调用
        ├── Span: chat claude-sonnet             ← 第二次推理（带工具结果）
        ├── Span: execute_tool read              ← 读文件
        ├── Span: execute_tool edit              ← 改文件
        ├── Span: invoke_agent sub-agent         ← 子 Agent
        │     ├── Span: chat claude-haiku
        │     └── Span: execute_tool grep
        └── Span: chat claude-sonnet             ← 最终回复
```

映射关系：

| 传统 Web | Agent |
|----------|-------|
| 一次 HTTP 请求 = Trace | 一次会话（session）= Trace |
| 一次服务调用 = Span | 一次 LLM 推理 / 一次工具调用 = Span |
| 服务间 RPC = 嵌套 Span | 子 Agent 调用 = 嵌套 Span |
| 状态码 + 延迟 | token 用量 + 费用 + 延迟 |

和传统可观测性一样，Agent 也有三大支柱：

| 支柱 | 在 Agent 场景的含义 |
|------|---------------------|
| **Traces** | 完整的调用链路：LLM 调了几次、每次调了哪些工具、耗时多少 |
| **Metrics** | 聚合指标：总 token、总费用、平均延迟、工具调用分布 |
| **Logs/Events** | 离散事件：重试、降级、压缩触发、权限拒绝 |

---

## OpenTelemetry GenAI 语义规范

OpenTelemetry（OTel）是可观测性领域的事实标准。2024 年起，OTel 社区开始制定专门的 **GenAI 语义规范**（Semantic Conventions for Generative AI），定义了 LLM 调用和 Agent 操作应该记录哪些信息。

> 目前仍处于 Development 阶段，但 LangChain、OpenAI Agents SDK、AWS Bedrock 等主流框架已开始采纳。

### Span 命名

OTel 规定 span 名称的格式：

| 操作类型 | Span 名称格式 | 示例 |
|----------|--------------|------|
| LLM 推理 | `{operation_name} {model}` | `chat claude-sonnet-4-20250514` |
| 工具执行 | `execute_tool {tool_name}` | `execute_tool bash` |
| Agent 调用 | `invoke_agent {agent_name}` | `invoke_agent code-reviewer` |
| 工作流 | `invoke_workflow {workflow_name}` | `invoke_workflow multi_agent_rag` |

### 核心属性

OTel 规范定义了一套标准化的属性名。以下是 Agent 场景最常用的：

**请求属性**（span 创建时设置）：

| 属性 | 类型 | 说明 |
|------|------|------|
| `gen_ai.operation.name` | string | 操作类型：`chat`, `execute_tool`, `invoke_agent` |
| `gen_ai.provider.name` | string | 提供商：`anthropic`, `openai`, `aws.bedrock` |
| `gen_ai.request.model` | string | 请求的模型名 |
| `gen_ai.request.max_tokens` | int | 最大输出 token |
| `gen_ai.request.temperature` | double | 温度参数 |
| `gen_ai.request.stream` | boolean | 是否流式 |
| `gen_ai.conversation.id` | string | 会话/对话 ID |

**响应属性**（span 结束时设置）：

| 属性 | 类型 | 说明 |
|------|------|------|
| `gen_ai.response.model` | string | 实际响应的模型（可能与请求不同） |
| `gen_ai.response.finish_reasons` | string[] | 停止原因：`stop`, `tool_use`, `max_tokens` |
| `gen_ai.response.id` | string | 响应 ID |
| `gen_ai.response.time_to_first_chunk` | double | 首 token 延迟（秒） |

**用量属性**（span 结束时设置）：

| 属性 | 类型 | 说明 |
|------|------|------|
| `gen_ai.usage.input_tokens` | int | 输入 token 总量（含缓存） |
| `gen_ai.usage.output_tokens` | int | 输出 token 总量 |
| `gen_ai.usage.cache_read.input_tokens` | int | 从缓存读取的 token |
| `gen_ai.usage.cache_creation.input_tokens` | int | 写入缓存的 token |

**Agent 属性**：

| 属性 | 类型 | 说明 |
|------|------|------|
| `gen_ai.agent.name` | string | Agent 名称 |
| `gen_ai.agent.id` | string | Agent 唯一标识 |

**工具属性**：

| 属性 | 类型 | 说明 |
|------|------|------|
| `gen_ai.tool.name` | string | 工具名称 |
| `gen_ai.tool.call.id` | string | 本次调用 ID |
| `gen_ai.tool.type` | string | 工具类型：`function`, `extension` |

### 标准 Metrics

| 指标名 | 类型 | 单位 | 说明 |
|--------|------|------|------|
| `gen_ai.client.token.usage` | Histogram | token | 每次调用的 token 用量，按 `gen_ai.token.type`（input/output）区分 |
| `gen_ai.client.operation.duration` | Histogram | 秒 | 端到端耗时（**唯一的 Required 指标**） |
| `gen_ai.client.operation.time_to_first_chunk` | Histogram | 秒 | 流式场景的首 token 延迟 |

注意 OTel 规范**没有定义费用指标**——费用是业务层关心的，不同模型定价不同，不适合标准化。这部分需要自己实现。

### 敏感数据处理

OTel 把消息内容（`gen_ai.input.messages`、`gen_ai.output.messages`、`gen_ai.system_instructions`）标记为 **Opt-In**——默认不采集。原因：

1. prompt 可能包含用户隐私
2. 工具调用结果可能包含源代码
3. 完整消息历史的体积很大

这和 Claude Code 的做法一致：它的 `logEvent()` 强制 metadata 值只能是 `boolean | number | undefined`，类型系统层面禁止记录字符串内容。

---

## 在哪里埋点

Agent 核心循环中有 5 个关键埋点位置：

### 1. LLM 调用（最核心）

```python
# ┌─────────────────────────────────────────────────────┐
# │  Span: chat {model}                                 │
# │  属性: request.model, request.temperature,           │
# │        request.max_tokens, request.stream            │
# │  结束时: usage.input_tokens, usage.output_tokens,    │
# │         response.finish_reasons, duration            │
# └─────────────────────────────────────────────────────┘

with tracer.start_span("chat", model=self.llm.model) as span:
    response = self.llm.chat(messages, tools=tools)
    span.set_attribute("gen_ai.usage.input_tokens", response.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", response.output_tokens)
    span.set_attribute("gen_ai.response.finish_reasons", response.stop_reason)
```

记录什么：模型名、token 用量、停止原因、耗时。这是**成本分析和性能优化**的核心数据源。

### 2. 工具执行

```python
# ┌─────────────────────────────────────────────────────┐
# │  Span: execute_tool {tool_name}                     │
# │  属性: tool.name, tool.call.id, tool.type            │
# │  结束时: duration, error.type (如有)                  │
# └─────────────────────────────────────────────────────┘

with tracer.start_span("execute_tool", tool_name=tool_use.name) as span:
    result = tool.execute(tool_use.input)
    span.set_attribute("result_length", len(result))
```

记录什么：工具名、调用 ID、耗时、结果大小。**不记录工具参数和返回值**（可能包含代码内容等敏感数据）。

### 3. 子 Agent 调用

```python
with tracer.start_span("invoke_agent", agent_name="code-reviewer") as span:
    sub_result = sub_agent.run(task)
    span.set_attribute("gen_ai.usage.input_tokens", sub_agent.total_input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", sub_agent.total_output_tokens)
```

子 Agent 内部的 LLM 调用和工具调用会自动成为这个 span 的子 span，形成嵌套结构。

### 4. 上下文压缩（自定义事件）

```python
tracer.add_event("auto_compact", {
    "original_tokens": before_tokens,
    "compressed_tokens": after_tokens,
    "compression_ratio": after_tokens / before_tokens,
})
```

不是 span（没有持续时间的意义），而是一个离散事件。

### 5. 重试与降级（自定义事件）

```python
tracer.add_event("llm_retry", {
    "attempt": attempt,
    "error_type": "rate_limit",
    "delay_seconds": delay,
})

tracer.add_event("model_fallback", {
    "from_model": "claude-sonnet",
    "to_model": "claude-haiku",
    "reason": "overloaded",
})
```

---

## 简化实现

基于 OTel 的概念，用纯 Python 实现一个轻量 Tracer。核心抽象是 **TraceSpan**——对齐 OTel 属性命名，但不依赖 OTel SDK。

### 数据结构

```python
import time
from dataclasses import dataclass, field
from contextlib import contextmanager


@dataclass
class TraceSpan:
    """一次操作的追踪记录（对齐 OTel GenAI 语义规范）"""
    name: str                        # "chat claude-sonnet" / "execute_tool bash"
    operation: str = ""              # gen_ai.operation.name
    start_time: float = 0.0
    end_time: float = 0.0
    attributes: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    children: list["TraceSpan"] = field(default_factory=list)
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        return (self.end_time - self.start_time) * 1000

    def set_attribute(self, key: str, value) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": attributes or {},
        })
```

### Tracer 类

```python
class Tracer:
    """会话级追踪器"""

    def __init__(self, session_id: str = ""):
        self.session_id = session_id
        self.spans: list[TraceSpan] = []
        self._stack: list[TraceSpan] = []
        self.session_start = time.time()

    @contextmanager
    def start_span(self, operation: str, **initial_attrs):
        """创建追踪 span（对齐 OTel 的 start_span 语义）"""
        # 构造 span 名称（OTel 格式: "{operation} {model/tool_name}"）
        model = initial_attrs.get("model", "")
        tool_name = initial_attrs.get("tool_name", "")
        agent_name = initial_attrs.get("agent_name", "")
        qualifier = model or tool_name or agent_name
        name = f"{operation} {qualifier}".strip()

        attrs = {"gen_ai.operation.name": operation}
        if model:
            attrs["gen_ai.request.model"] = model
        if tool_name:
            attrs["gen_ai.tool.name"] = tool_name
        if agent_name:
            attrs["gen_ai.agent.name"] = agent_name

        span = TraceSpan(
            name=name,
            operation=operation,
            start_time=time.time(),
            attributes=attrs,
        )

        if self._stack:
            self._stack[-1].children.append(span)
        else:
            self.spans.append(span)

        self._stack.append(span)
        try:
            yield span
        except Exception as e:
            span.error = str(e)
            span.set_attribute("error.type", type(e).__name__)
            raise
        finally:
            span.end_time = time.time()
            self._stack.pop()

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        """记录一个离散事件（挂在当前 span 上，或挂在顶层）"""
        if self._stack:
            self._stack[-1].add_event(name, attributes)
        else:
            self.spans.append(TraceSpan(
                name=f"event:{name}",
                operation="event",
                start_time=time.time(),
                end_time=time.time(),
                events=[{"name": name, "timestamp": time.time(),
                         "attributes": attributes or {}}],
            ))
```

### Agent 集成

在 Agent 核心循环中埋点：

```python
class Agent:
    def __init__(self, ...):
        self.tracer = Tracer(session_id=self.session_id)

    def _run_tool_loop(self, prompt: str):
        while True:
            # 埋点 1: LLM 调用
            with self.tracer.start_span("chat", model=self.llm.model) as span:
                span.set_attribute("gen_ai.provider.name", self.llm.provider)
                span.set_attribute("gen_ai.request.max_tokens", self.llm.max_tokens)
                span.set_attribute("gen_ai.conversation.id", self.session_id)

                response = self.llm.chat(self.messages, tools=self.tool_schemas)

                span.set_attribute("gen_ai.usage.input_tokens", response.input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", response.output_tokens)
                span.set_attribute("gen_ai.usage.cache_read.input_tokens",
                                   response.cache_read_tokens)
                span.set_attribute("gen_ai.response.finish_reasons",
                                   [response.stop_reason])

            if not response.has_tool_use:
                break

            # 埋点 2: 工具调用
            for tool_use in response.tool_uses:
                with self.tracer.start_span(
                    "execute_tool", tool_name=tool_use["name"]
                ) as tool_span:
                    tool_span.set_attribute("gen_ai.tool.call.id", tool_use["id"])
                    result = self._execute_tool(tool_use)
                    tool_span.set_attribute("result_length", len(result))

    # 埋点 3: 子 Agent（在 AgentTool 中）
    def _spawn_sub_agent(self, task: str, agent_name: str):
        with self.tracer.start_span("invoke_agent", agent_name=agent_name):
            sub_agent = Agent(...)
            return sub_agent.chat(task)

    # 埋点 4: 上下文压缩
    def _auto_compact(self):
        before = self._count_tokens()
        self.messages = self._compact(self.messages)
        after = self._count_tokens()
        self.tracer.add_event("auto_compact", {
            "original_tokens": before,
            "compressed_tokens": after,
        })
```

### 会话报告

```python
class Tracer:
    # ... 接上面

    def summary(self) -> dict:
        """生成会话级汇总（对应 OTel Metrics 层）"""
        all_spans = self._flatten(self.spans)
        llm_spans = [s for s in all_spans if s.operation == "chat"]
        tool_spans = [s for s in all_spans if s.operation == "execute_tool"]

        total_input = sum(
            s.attributes.get("gen_ai.usage.input_tokens", 0) for s in llm_spans)
        total_output = sum(
            s.attributes.get("gen_ai.usage.output_tokens", 0) for s in llm_spans)
        total_cache = sum(
            s.attributes.get("gen_ai.usage.cache_read.input_tokens", 0)
            for s in llm_spans)

        # 工具调用分布
        tool_counts: dict[str, int] = {}
        for s in tool_spans:
            tool_counts[s.name] = tool_counts.get(s.name, 0) + 1

        # 最慢 top-5
        slowest = sorted(all_spans, key=lambda s: s.duration_ms, reverse=True)[:5]

        return {
            "duration_s": time.time() - self.session_start,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read_tokens": total_cache,
            "llm_calls": len(llm_spans),
            "tool_calls": len(tool_spans),
            "tool_distribution": tool_counts,
            "slowest_operations": [
                {"name": s.name, "duration_ms": s.duration_ms} for s in slowest
            ],
            "errors": [s for s in all_spans if s.error],
        }

    def _flatten(self, spans: list[TraceSpan]) -> list[TraceSpan]:
        result = []
        for s in spans:
            result.append(s)
            result.extend(self._flatten(s.children))
        return result
```

输出效果：

```
──────────────────────────────────────────────────
Duration:           28.7s
LLM calls:          4
Tool calls:         7
Tokens:             8,450 in / 2,100 out / 6,200 cache

Tool distribution:
  execute_tool bash    3x
  execute_tool read    2x
  execute_tool edit    1x
  execute_tool grep    1x

Slowest operations:
  chat claude-sonnet   3,240ms
  chat claude-sonnet   2,890ms
  execute_tool bash    1,205ms
  chat claude-sonnet   1,102ms
  execute_tool grep      340ms
──────────────────────────────────────────────────
```

### 费用计算（OTel 之外的扩展）

OTel 规范不包含费用指标，需要自行实现：

```python
# 定价表（每百万 token）
PRICING = {
    "claude-sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
    "claude-haiku": {"input": 0.25, "output": 1.25, "cache_read": 0.03},
}

def calculate_cost(span: TraceSpan) -> float:
    """从 span 属性中计算费用"""
    model = span.attributes.get("gen_ai.request.model", "")
    pricing = PRICING.get(model, PRICING["claude-sonnet"])

    input_t = span.attributes.get("gen_ai.usage.input_tokens", 0)
    output_t = span.attributes.get("gen_ai.usage.output_tokens", 0)
    cache_t = span.attributes.get("gen_ai.usage.cache_read.input_tokens", 0)

    return (
        input_t * pricing["input"] / 1_000_000
        + output_t * pricing["output"] / 1_000_000
        + cache_t * pricing["cache_read"] / 1_000_000
    )
```

---

## Claude Code 的实现

Claude Code 没有使用 OTel，而是自建了一套可观测性体系。了解它的设计选择，有助于理解"标准方案"和"自研方案"的取舍。

### 成本追踪

Claude Code 在全局 state 中维护累计计数器，而非逐 span 记录：

```typescript
// Claude Code: bootstrap/state.ts
interface CostState {
  totalInputTokens: number
  totalOutputTokens: number
  totalCacheReadInputTokens: number
  totalCacheCreationInputTokens: number
  totalCostUSD: number
  totalAPIDuration: number
  totalAPIDurationWithoutRetries: number
  totalToolDuration: number
  totalLinesAdded: number
  totalLinesRemoved: number
  modelUsage: { [model: string]: ModelUsage }
}
```

每次 API 调用后累加，会话结束时输出汇总。这种设计比 span 树更简单，但丢失了调用级的细节。

### 事件系统

```typescript
// Claude Code: services/analytics/index.ts
export function logEvent(
  eventName: string,
  metadata: { [key: string]: boolean | number | undefined },
): void
```

设计要点：
- **无字符串 metadata** — 类型系统强制只能传 `boolean | number | undefined`，防止泄露代码或路径
- **延迟初始化** — sink 启动后才 attach，之前的事件排队
- **双写** — 同时发往 Datadog（实时）和 1P logger（持久）

关键埋点包括：`auto_compact_succeeded`（压缩）、`model_fallback_triggered`（降级）、`query_error`（异常）、`max_tokens_escalate`（输出截断升级）等。

### 会话持久化

```typescript
// Claude Code: cost-tracker.ts
export function saveCurrentSessionCosts(): void {
  saveCurrentProjectConfig(current => ({
    ...current,
    lastCost: getTotalCostUSD(),
    lastAPIDuration: getTotalAPIDuration(),
    lastSessionId: getSessionId(),
    lastModelUsage: getModelUsage(),
  }))
}
```

resume 会话时恢复之前的累计值，用户看到的始终是该会话的**总成本**。

### 为什么 Claude Code 没用 OTel？

| 考量 | OTel | 自研 |
|------|------|------|
| 依赖 | 需要 SDK + Exporter 包 | 零依赖 |
| 灵活度 | 受规范约束 | 完全自定义 |
| 隐私控制 | 需配置 Processor 过滤 | 类型系统直接限制 |
| 后端对接 | 标准 OTLP 协议 | 直连 Datadog API |
| 适用场景 | 多团队/多语言/多服务 | 单一产品、内部闭环 |

Claude Code 作为单一 CLI 产品，不需要跨服务的标准化 trace 传播。自研方案更轻量、隐私控制更严格。但对于**自建的 Agent 平台**（多用户、多 Agent、需要可视化 trace），OTel 是更好的选择。

---

## 三方对比

| 维度 | OTel GenAI 规范 | Claude Code | 我们的实现 |
|------|-----------------|-------------|------------|
| Span 模型 | 标准 trace/span 树 | 无 span，全局累加计数器 | span 树（对齐 OTel 命名） |
| 属性命名 | `gen_ai.*` 命名空间 | 自定义字段名 | 沿用 `gen_ai.*` |
| 费用追踪 | 不在规范内 | 全局 `totalCostUSD` | 基于 span 属性计算 |
| 事件/日志 | span events | `logEvent()` → Datadog | `add_event()` → 内存 |
| 敏感数据 | Opt-In 机制 | 类型系统禁止 string | 不记录消息内容 |
| Metrics | Histogram（token、延迟） | 无独立 metrics，靠计数器 | summary() 汇总 |
| 持久化 | Exporter → 后端 | projectConfig 文件 | 未持久化（可扩展） |
| 跨服务 | OTLP 传播 | 不需要 | 不涉及 |
| 代码量 | SDK 约 500+ 行配置 | ~250 行 cost-tracker + ~200 行 analytics | ~150 行 Python |

---

## 小结

Agent 可观测性回答三个问题：

1. **花了多少？** — token 用量 × 单价，按模型/会话累计
2. **慢在哪？** — span 计时，LLM 推理 vs 工具执行，找瓶颈
3. **发生了什么？** — 事件打点：压缩、重试、降级、报错

核心设计原则：

- **Span 是基本单位** — 每个 LLM 调用和工具执行对应一个 span，支持嵌套
- **属性对齐标准** — 用 OTel `gen_ai.*` 命名空间，方便将来接入标准后端
- **不记录内容，只记录维度** — 防止敏感数据泄露到日志
- **费用是扩展，不是标准** — OTel 不管钱的事，需要自己算

如果是自用的小工具，像 Claude Code 一样搞几个全局计数器就够了。如果是面向多用户的 Agent 平台，建议从第一天就用 OTel 规范——属性命名、span 结构、指标定义都有现成标准，不用自己发明轮子。

下一篇我们来实现 **Agent 评测**——怎么系统地度量 Agent 改好了还是改差了。
