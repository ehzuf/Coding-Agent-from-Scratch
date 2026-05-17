# 从零实现 Coding Agent（二十七）：可观测性与 Tracing

Agent 跑了一圈回来告诉你"任务完成"，你可能想知道：

- 这一轮花了多少钱？
- 哪个工具调用最慢？
- 为什么它选了这个方案而不是那个？
- 重试了几次？中间有没有报错？

传统软件有日志、有 APM、有链路追踪。Agent 也需要类似的可观测性——但维度不同：它的核心单位不是 HTTP 请求，而是 **LLM 调用**和**工具执行**。

这一篇拆解 Claude Code 的可观测性设计，它由三个层次组成：成本追踪、事件分析、会话级统计。

---

## Claude Code 的可观测性架构

```
Agent 运行时
  ├── cost-tracker.ts (累计 token/费用/耗时/行数)
  ├── bootstrap/state.ts (全局状态容器)
  ├── services/analytics/ (事件打点 → Datadog + 1P)
  └── query.ts 中的 logEvent() 调用 (关键路径埋点)
```

### 第一层：成本追踪

Claude Code 在全局 state 中维护一组累计计数器：

```typescript
// Claude Code: bootstrap/state.ts
interface CostState {
  totalInputTokens: number
  totalOutputTokens: number
  totalCacheReadInputTokens: number
  totalCacheCreationInputTokens: number
  totalCostUSD: number
  totalAPIDuration: number                // LLM API 耗时累计
  totalAPIDurationWithoutRetries: number  // 减去重试的纯耗时
  totalToolDuration: number               // 工具执行耗时累计
  totalLinesAdded: number
  totalLinesRemoved: number
  totalWebSearchRequests: number
  modelUsage: { [model: string]: ModelUsage }  // 按模型分计
}
```

每次 LLM 调用返回时，更新这些计数器：

```typescript
// 每次 API 调用后
addToTotalAPIDuration(duration, durationWithoutRetries)

// 每次工具执行后
addToTotalToolDuration(toolDuration)

// 每次修改文件后
addToTotalLinesChanged(linesAdded, linesRemoved)
```

会话结束时输出汇总：

```
Total cost:            $0.0847
Total duration (API):  45.2s
Total duration (wall): 1m 23s
Total code changes:    42 lines added, 17 lines removed
Usage by model:
       claude-sonnet:  12,450 input, 3,200 output, 8,900 cache read, 1,200 cache write ($0.0847)
```

### 第二层：事件分析

Claude Code 有完整的事件打点系统：

```typescript
// Claude Code: services/analytics/index.ts
export function logEvent(
  eventName: string,
  metadata: { [key: string]: boolean | number | undefined },
): void {
  if (sink === null) {
    eventQueue.push({ eventName, metadata, async: false })
    return
  }
  sink.logEvent(eventName, metadata)
}
```

设计要点：

1. **延迟初始化** — sink 在 app 启动后才 attach，之前的事件排队等待
2. **无字符串 metadata** — 类型系统强制 metadata 值只能是 `boolean | number | undefined`，防止意外记录代码或文件路径等敏感数据
3. **异步刷新** — drain queue 用 `queueMicrotask` 避免阻塞启动路径
4. **双写** — 同时发往 Datadog（实时监控）和 1P event logger（长期存储）

#### 关键埋点

从 `query.ts` 中可以看到 Claude Code 在哪些路径打点：

| 事件名 | 触发时机 | 用途 |
|--------|----------|------|
| `tengu_auto_compact_succeeded` | 自动压缩完成 | 监控压缩频率和效果 |
| `tengu_model_fallback_triggered` | 主模型失败降级 | 追踪模型可用性 |
| `tengu_query_error` | query 循环异常 | 错误率告警 |
| `tengu_streaming_tool_execution_used` | 使用了流式工具执行 | feature 采用率 |
| `tengu_max_tokens_escalate` | max_tokens 自动升级 | 观察输出截断频率 |
| `tengu_token_budget_completed` | token budget 消耗完毕 | 使用量分布 |
| `tengu_post_autocompact_turn` | 压缩后的首次对话 | 压缩后质量监控 |

### 第三层：会话级持久化

成本数据在会话结束时持久化，支持 resume 后累计：

```typescript
// Claude Code: cost-tracker.ts
export function saveCurrentSessionCosts(): void {
  saveCurrentProjectConfig(current => ({
    ...current,
    lastCost: getTotalCostUSD(),
    lastAPIDuration: getTotalAPIDuration(),
    lastToolDuration: getTotalToolDuration(),
    lastLinesAdded: getTotalLinesAdded(),
    lastLinesRemoved: getTotalLinesRemoved(),
    lastSessionId: getSessionId(),
    lastModelUsage: getModelUsage(),
  }))
}

export function restoreCostStateForSession(sessionId: string): boolean {
  const data = getStoredSessionCosts(sessionId)
  if (!data) return false
  setCostStateForRestore(data)
  return true
}
```

这样 resume 一个会话时，之前的花费继续累加，用户看到的始终是该会话的**总成本**。

---

## 简化实现

我们实现一个轻量的 Tracing 系统，核心抽象是 **TraceSpan**——每个 LLM 调用和工具执行对应一个 span。

### 数据结构

```python
import time
from dataclasses import dataclass, field
from contextlib import contextmanager


@dataclass
class TraceSpan:
    """一次操作的追踪记录"""
    name: str                           # "llm_call" / "tool:bash" / "tool:read"
    start_time: float = 0.0
    end_time: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    metadata: dict = field(default_factory=dict)  # 自定义数据
    children: list["TraceSpan"] = field(default_factory=list)
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        return (self.end_time - self.start_time) * 1000

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
```

### Tracer 类

```python
class Tracer:
    """会话级追踪器"""

    def __init__(self):
        self.spans: list[TraceSpan] = []
        self._stack: list[TraceSpan] = []  # 嵌套支持
        self.session_start = time.time()

    @contextmanager
    def span(self, name: str, **metadata):
        """创建一个追踪 span（context manager）"""
        s = TraceSpan(name=name, start_time=time.time(), metadata=metadata)

        # 嵌套：如果有父 span，加入其 children
        if self._stack:
            self._stack[-1].children.append(s)
        else:
            self.spans.append(s)

        self._stack.append(s)
        try:
            yield s
        except Exception as e:
            s.error = str(e)
            raise
        finally:
            s.end_time = time.time()
            self._stack.pop()

    def record_llm_call(self, span: TraceSpan, response) -> None:
        """记录 LLM 调用结果"""
        span.input_tokens = response.input_tokens
        span.output_tokens = response.output_tokens
        span.cache_read_tokens = response.cache_read_tokens
        span.cost_usd = self._calculate_cost(response)

    def _calculate_cost(self, response) -> float:
        """简化的费用计算（Sonnet 定价）"""
        input_cost = response.input_tokens * 3.0 / 1_000_000
        output_cost = response.output_tokens * 15.0 / 1_000_000
        cache_cost = response.cache_read_tokens * 0.3 / 1_000_000
        return input_cost + output_cost + cache_cost
```

### Agent 集成

在 Agent 的核心循环中自动创建 span：

```python
class Agent:
    def __init__(self, ...):
        self.tracer = Tracer()

    def _run_tool_loop(self, prompt: str):
        while True:
            # 追踪 LLM 调用
            with self.tracer.span("llm_call", model=self.llm.model) as s:
                response = self.llm.chat(self.messages, ...)
                self.tracer.record_llm_call(s, response)

            if not response.has_tool_use:
                break

            # 追踪每个工具调用
            for tool_use in response.tool_uses:
                with self.tracer.span(
                    f"tool:{tool_use['name']}",
                    input_keys=list(tool_use["input"].keys()),
                ) as ts:
                    result = self._execute_tool(tool_use)
                    ts.metadata["result_length"] = len(result)
```

### 会话报告

```python
class Tracer:
    # ... 接上面

    def summary(self) -> dict:
        """生成会话级汇总"""
        all_spans = self._flatten(self.spans)
        llm_spans = [s for s in all_spans if s.name == "llm_call"]
        tool_spans = [s for s in all_spans if s.name.startswith("tool:")]

        total_cost = sum(s.cost_usd for s in llm_spans)
        total_input = sum(s.input_tokens for s in llm_spans)
        total_output = sum(s.output_tokens for s in llm_spans)
        total_cache = sum(s.cache_read_tokens for s in llm_spans)

        # 工具调用分布
        tool_counts: dict[str, int] = {}
        for s in tool_spans:
            tool_counts[s.name] = tool_counts.get(s.name, 0) + 1

        # 最慢 top-5
        slowest = sorted(all_spans, key=lambda s: s.duration_ms, reverse=True)[:5]

        return {
            "duration_s": time.time() - self.session_start,
            "total_cost_usd": total_cost,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read_tokens": total_cache,
            "llm_calls": len(llm_spans),
            "tool_calls": len(tool_spans),
            "tool_distribution": tool_counts,
            "slowest_operations": [
                {"name": s.name, "duration_ms": s.duration_ms}
                for s in slowest
            ],
            "errors": [s for s in all_spans if s.error],
        }

    def _flatten(self, spans: list[TraceSpan]) -> list[TraceSpan]:
        """递归展平 span 树"""
        result = []
        for s in spans:
            result.append(s)
            result.extend(self._flatten(s.children))
        return result
```

### 输出格式

```python
def print_summary(tracer: Tracer) -> None:
    """打印会话追踪摘要"""
    s = tracer.summary()
    print(f"\n{'─' * 50}")
    print(f"Total cost:         ${s['total_cost_usd']:.4f}")
    print(f"Duration:           {s['duration_s']:.1f}s")
    print(f"LLM calls:          {s['llm_calls']}")
    print(f"Tool calls:         {s['tool_calls']}")
    print(f"Tokens:             {s['total_input_tokens']:,} in / "
          f"{s['total_output_tokens']:,} out / "
          f"{s['total_cache_read_tokens']:,} cache")
    print(f"\nTool distribution:")
    for name, count in sorted(s["tool_distribution"].items(),
                               key=lambda x: -x[1]):
        print(f"  {name:20s} {count}x")
    print(f"\nSlowest operations:")
    for op in s["slowest_operations"]:
        print(f"  {op['name']:20s} {op['duration_ms']:.0f}ms")
    if s["errors"]:
        print(f"\nErrors: {len(s['errors'])}")
        for err_span in s["errors"][:3]:
            print(f"  [{err_span.name}] {err_span.error}")
    print(f"{'─' * 50}")
```

运行效果：

```
──────────────────────────────────────────────────
Total cost:         $0.0342
Duration:           28.7s
LLM calls:          4
Tool calls:         7
Tokens:             8,450 in / 2,100 out / 6,200 cache

Tool distribution:
  tool:bash              3x
  tool:read              2x
  tool:edit              1x
  tool:grep              1x

Slowest operations:
  llm_call             3,240ms
  llm_call             2,890ms
  tool:bash            1,205ms
  llm_call             1,102ms
  tool:grep              340ms
──────────────────────────────────────────────────
```

---

## 事件系统（选做扩展）

如果需要更丰富的分析，可以加一个简单的事件系统：

```python
from collections import defaultdict


class EventLogger:
    """轻量事件记录器"""

    def __init__(self):
        self._events: list[dict] = []
        self._counts: dict[str, int] = defaultdict(int)

    def log(self, event_name: str, **metadata) -> None:
        self._events.append({
            "event": event_name,
            "timestamp": time.time(),
            **{k: v for k, v in metadata.items()
               if isinstance(v, (bool, int, float))},
        })
        self._counts[event_name] += 1

    def get_counts(self) -> dict[str, int]:
        return dict(self._counts)
```

在关键路径打点：

```python
# 压缩触发时
event_logger.log("auto_compact", original_tokens=before, new_tokens=after)

# 重试时
event_logger.log("llm_retry", attempt=attempt, delay_s=delay)

# 权限拒绝时
event_logger.log("permission_denied", tool=tool_name)

# 模型降级时
event_logger.log("model_fallback", from_model=primary, to_model=fallback)
```

---

## 与 Claude Code 的对比

| 维度 | Claude Code | 我们的实现 |
|------|-------------|------------|
| 成本追踪 | 全局 state 累计，按模型分计 | TraceSpan 逐调用记录，summary 汇总 |
| 事件系统 | `logEvent()` → Datadog + 1P，类型安全 | `EventLogger.log()` → 内存列表 |
| 持久化 | `saveCurrentSessionCosts()` → projectConfig | 未持久化（可扩展到 session.jsonl） |
| 耗时追踪 | 三类：API / API无重试 / 工具 | span 统一记录，按 name 区分 |
| 行数统计 | `addToTotalLinesChanged` 全局计数 | 可在 edit/write 工具 span 中记录 |
| resume 累计 | 持久化后 restore | 未实现 |
| 采样 | 动态采样率（GrowthBook 控制） | 无采样（全量记录） |
| 隐私保护 | 类型系统禁止 string metadata | 运行时过滤 |
| 代码量 | ~250 行 cost-tracker + ~200 行 analytics | ~120 行 Python |

---

## 小结

可观测性回答三个问题：

1. **花了多少？** — 成本追踪（token × 单价，按模型/会话累计）
2. **慢在哪？** — Span 计时（LLM 调用 vs 工具执行，找瓶颈）
3. **发生了什么？** — 事件打点（压缩、重试、降级、报错等关键路径）

核心设计思想：
- **Span 是基本单位** — 每个操作有开始、结束、结果，支持嵌套
- **不记录内容，只记录维度** — 防止敏感数据泄露到日志
- **会话粒度汇总** — 用户关心的是"这次对话"的总成本，不是单次 API 调用

下一篇我们来实现 **Agent 评测**——怎么系统地度量 Agent 改好了还是改差了。
