# 从 Coding Agent 到个人助理（三）：成本追踪与使用洞察

上一篇我们用智能路由和辅助 LLM 优化了成本。但有一个问题——到底省了多少？不知道。用了多少 token？不知道。哪个模型最贵？不知道。

本篇实现**成本追踪系统**，让每一笔 LLM 调用都有据可查，并提供 `/insights` 命令生成使用分析报告。

## 为什么需要成本追踪

LLM API 按 token 计费，不同模型价格差异巨大：

| 模型 | 输入价格 ($/M tokens) | 输出价格 ($/M tokens) |
|---|---|---|
| GPT-4o-mini | 0.15 | 0.60 |
| Claude Sonnet 4 | 3.00 | 15.00 |
| Claude Opus 4 | 15.00 | 75.00 |
| o3 | 10.00 | 40.00 |

一次长对话（10 轮工具调用）可能消耗 10 万+ token，费用从 $0.01 到 $1+ 不等。如果不追踪，很容易在不知不觉中产生高额费用。

## 数据模型设计

### 规范化 Usage

不同 LLM 提供商返回的 usage 数据格式不同。Anthropic 返回 `cache_read_input_tokens`，OpenAI 返回 `prompt_tokens_details.cached_tokens`。我们需要一个统一的数据结构：

```python
# agent/usage_tracker.py

from dataclasses import dataclass, field
from decimal import Decimal

@dataclass(frozen=True)
class Usage:
    """规范化的 LLM 使用量数据"""
    input_tokens: int = 0          # 输入 token（不含缓存）
    output_tokens: int = 0         # 输出 token
    cache_read_tokens: int = 0     # 缓存读取的 token（节省费用）
    cache_write_tokens: int = 0    # 写入缓存的 token（Anthropic 特有）
    reasoning_tokens: int = 0      # 推理 token（o3 等思考模型）

    @property
    def total_tokens(self):
        return (self.input_tokens + self.output_tokens +
                self.cache_read_tokens + self.cache_write_tokens +
                self.reasoning_tokens)
```

五个字段覆盖了主流模型的所有计费维度：

- `input_tokens` + `output_tokens`：最基本的输入输出
- `cache_read_tokens`：Prompt Caching 命中的部分，价格通常是输入的 1/10
- `cache_write_tokens`：Anthropic 的缓存写入，价格是输入的 1.25 倍
- `reasoning_tokens`：o3、DeepSeek-R1 等模型的"思考"token

### 从 API 响应提取 Usage

```python
def normalize_usage(raw_usage, provider="anthropic"):
    """从 LLM API 响应中提取规范化 Usage"""
    if raw_usage is None:
        return Usage()

    if provider == "anthropic":
        return Usage(
            input_tokens=raw_usage.get("input_tokens", 0),
            output_tokens=raw_usage.get("output_tokens", 0),
            cache_read_tokens=raw_usage.get("cache_read_input_tokens", 0),
            cache_write_tokens=raw_usage.get("cache_creation_input_tokens", 0),
        )

    # OpenAI 格式
    prompt_details = raw_usage.get("prompt_tokens_details") or {}
    completion_details = raw_usage.get("completion_tokens_details") or {}

    cached = prompt_details.get("cached_tokens", 0)
    reasoning = completion_details.get("reasoning_tokens", 0)

    return Usage(
        input_tokens=raw_usage.get("prompt_tokens", 0) - cached,
        output_tokens=raw_usage.get("completion_tokens", 0) - reasoning,
        cache_read_tokens=cached,
        reasoning_tokens=reasoning,
    )
```

注意 OpenAI 的 `prompt_tokens` 已经**包含**了缓存 token，需要减去才能得到真实的输入量。

### 模型定价表

成本计算需要知道每个模型的单价。我们内置一份主流模型的定价快照：

```python
# 价格单位：美元 / 百万 token
_PRICING_TABLE = {
    # (provider, model): {input, output, cache_read, cache_write}
    ("anthropic", "claude-sonnet-4-20250514"): {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
        "cache_read": Decimal("0.30"),
        "cache_write": Decimal("3.75"),
    },
    ("anthropic", "claude-opus-4-20250514"): {
        "input": Decimal("15.00"),
        "output": Decimal("75.00"),
        "cache_read": Decimal("1.50"),
        "cache_write": Decimal("18.75"),
    },
    ("openai", "gpt-4o"): {
        "input": Decimal("2.50"),
        "output": Decimal("10.00"),
        "cache_read": Decimal("1.25"),
    },
    ("openai", "gpt-4o-mini"): {
        "input": Decimal("0.15"),
        "output": Decimal("0.60"),
        "cache_read": Decimal("0.075"),
    },
    ("openai", "o3"): {
        "input": Decimal("10.00"),
        "output": Decimal("40.00"),
        "cache_read": Decimal("2.50"),
    },
    ("openai", "o3-mini"): {
        "input": Decimal("1.10"),
        "output": Decimal("4.40"),
        "cache_read": Decimal("0.55"),
    },
}
```

用 `Decimal` 而不是 `float`，避免浮点精度问题（$0.075 用 float 存储会有误差）。

### 成本计算

```python
_ONE_MILLION = Decimal("1000000")

def estimate_cost(usage, provider, model):
    """根据 Usage 和模型定价计算费用（美元）
    
    Returns:
        (amount_usd, status)
        status: "estimated" 表示使用内置价格表
                "unknown" 表示模型不在价格表中（返回 0）
    """
    pricing = _PRICING_TABLE.get((provider, model))

    if pricing is None:
        # 尝试模糊匹配（去掉日期后缀）
        for (p, m), price in _PRICING_TABLE.items():
            if p == provider and model.startswith(m.split("-")[0]):
                pricing = price
                break

    if pricing is None:
        return Decimal("0"), "unknown"

    cost = Decimal("0")
    cost += Decimal(usage.input_tokens) * pricing.get("input", Decimal("0")) / _ONE_MILLION
    cost += Decimal(usage.output_tokens) * pricing.get("output", Decimal("0")) / _ONE_MILLION
    cost += Decimal(usage.cache_read_tokens) * pricing.get("cache_read", Decimal("0")) / _ONE_MILLION
    cost += Decimal(usage.cache_write_tokens) * pricing.get("cache_write", Decimal("0")) / _ONE_MILLION

    # 推理 token 通常按输出价格计费
    if usage.reasoning_tokens > 0:
        cost += Decimal(usage.reasoning_tokens) * pricing.get("output", Decimal("0")) / _ONE_MILLION

    return cost, "estimated"
```

模糊匹配是为了处理模型名带日期后缀的情况——`claude-sonnet-4-20250514` 和 `claude-sonnet-4` 应该用同一个价格。

## 会话级追踪器

有了 Usage 和成本计算，接下来构建会话级的追踪器：

```python
@dataclass
class SessionUsage:
    """一次会话的累计使用量"""
    model: str = ""
    provider: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cost_usd: Decimal = Decimal("0")
    call_count: int = 0

    @property
    def total_tokens(self):
        return (self.total_input_tokens + self.total_output_tokens +
                self.total_cache_read_tokens + self.total_cache_write_tokens +
                self.total_reasoning_tokens)


class UsageTracker:
    """追踪 LLM API 使用量和成本"""

    def __init__(self):
        self.sessions: dict[str, SessionUsage] = {}  # session_id -> usage
        self.current_session_id: str | None = None

    def start_session(self, session_id, model, provider):
        """开始新会话"""
        self.current_session_id = session_id
        self.sessions[session_id] = SessionUsage(model=model, provider=provider)

    def record(self, usage: Usage, provider: str, model: str):
        """记录一次 LLM 调用的使用量"""
        if self.current_session_id is None:
            return

        session = self.sessions[self.current_session_id]
        session.total_input_tokens += usage.input_tokens
        session.total_output_tokens += usage.output_tokens
        session.total_cache_read_tokens += usage.cache_read_tokens
        session.total_cache_write_tokens += usage.cache_write_tokens
        session.total_reasoning_tokens += usage.reasoning_tokens
        session.call_count += 1

        cost, status = estimate_cost(usage, provider, model)
        session.total_cost_usd += cost

    def get_current_summary(self):
        """获取当前会话摘要"""
        if self.current_session_id is None:
            return None
        return self.sessions.get(self.current_session_id)
```

### 集成到 Agent 主循环

在每次 LLM 调用后记录使用量：

```python
# agent/agent.py

class Agent:
    def __init__(self, config):
        # ... 原有初始化 ...
        self.usage_tracker = UsageTracker()

    def _call_llm(self, messages, system=None, tools=None):
        """调用 LLM 并记录使用量"""
        response = self.llm.chat(
            messages=messages,
            system=system,
            tools=tools,
        )

        # 记录使用量
        usage = normalize_usage(response.raw_usage, provider=self.provider)
        self.usage_tracker.record(usage, self.provider, self.model)

        return response
```

在每轮对话结束后显示当前费用：

```python
def _show_usage_summary(self):
    """显示当前会话的使用量摘要"""
    summary = self.usage_tracker.get_current_summary()
    if summary is None:
        return

    tokens = summary.total_tokens
    cost = summary.total_cost_usd

    if cost > 0:
        print(f"📊 Tokens: {tokens:,} | Cost: ${cost:.4f} | Calls: {summary.call_count}")
    else:
        print(f"📊 Tokens: {tokens:,} | Cost: unknown | Calls: {summary.call_count}")
```

## 使用洞察（Insights）

追踪数据本身的价值有限，关键是能从中提取洞察。实现一个 `/insights` 命令，生成多维分析报告：

```python
class InsightsEngine:
    """从使用量数据生成分析报告"""

    def __init__(self, tracker: UsageTracker):
        self.tracker = tracker

    def generate_report(self):
        """生成使用分析报告"""
        sessions = list(self.tracker.sessions.values())
        if not sessions:
            return "No usage data yet."

        lines = ["📊 Usage Insights", "=" * 40, ""]

        # 总览
        total_tokens = sum(s.total_tokens for s in sessions)
        total_cost = sum(s.total_cost_usd for s in sessions)
        total_calls = sum(s.call_count for s in sessions)

        lines.append("## Overview")
        lines.append(f"  Sessions: {len(sessions)}")
        lines.append(f"  Total tokens: {total_tokens:,}")
        lines.append(f"  Total cost: ${total_cost:.4f}")
        lines.append(f"  Total LLM calls: {total_calls}")
        lines.append("")

        # 模型分布
        model_stats = {}
        for s in sessions:
            key = f"{s.model} ({s.provider})"
            if key not in model_stats:
                model_stats[key] = {"tokens": 0, "cost": Decimal("0"), "calls": 0}
            model_stats[key]["tokens"] += s.total_tokens
            model_stats[key]["cost"] += s.total_cost_usd
            model_stats[key]["calls"] += s.call_count

        lines.append("## Model Breakdown")
        for model, stats in sorted(model_stats.items(), key=lambda x: x[1]["cost"], reverse=True):
            pct = stats["tokens"] / total_tokens * 100 if total_tokens > 0 else 0
            lines.append(f"  {model}")
            lines.append(f"    Tokens: {stats['tokens']:,} ({pct:.1f}%)")
            lines.append(f"    Cost: ${stats['cost']:.4f}")
            lines.append(f"    Calls: {stats['calls']}")
        lines.append("")

        # 缓存效率
        total_cache_read = sum(s.total_cache_read_tokens for s in sessions)
        total_input = sum(s.total_input_tokens + s.total_cache_read_tokens for s in sessions)
        if total_input > 0:
            cache_hit_rate = total_cache_read / total_input * 100
            lines.append("## Cache Efficiency")
            lines.append(f"  Cache hit rate: {cache_hit_rate:.1f}%")
            lines.append(f"  Cached tokens: {total_cache_read:,}")

            # 估算节省的费用
            if total_cache_read > 0:
                # 假设没有缓存时这些 token 按输入价格计费
                avg_input_price = total_cost / Decimal(max(total_tokens, 1)) * _ONE_MILLION
                saved = Decimal(total_cache_read) * avg_input_price * Decimal("0.9") / _ONE_MILLION
                lines.append(f"  Estimated savings: ~${saved:.4f}")
            lines.append("")

        # 最贵的会话
        expensive = sorted(sessions, key=lambda s: s.total_cost_usd, reverse=True)[:5]
        if expensive and expensive[0].total_cost_usd > 0:
            lines.append("## Top Sessions by Cost")
            for i, s in enumerate(expensive, 1):
                lines.append(f"  {i}. {s.model}: {s.total_tokens:,} tokens, ${s.total_cost_usd:.4f}")

        return "\n".join(lines)
```

### 注册 /insights 命令

在 REPL 循环中识别斜杠命令：

```python
# agent/__main__.py

while True:
    user_input = input("> ").strip()

    if user_input == "/insights":
        engine = InsightsEngine(agent.usage_tracker)
        print(engine.generate_report())
        continue

    if user_input == "/usage":
        agent._show_usage_summary()
        continue

    # ... 正常对话 ...
```

### 报告示例

```
📊 Usage Insights
========================================

## Overview
  Sessions: 3
  Total tokens: 45,230
  Total cost: $0.1847
  Total LLM calls: 12

## Model Breakdown
  claude-sonnet-4-20250514 (anthropic)
    Tokens: 38,500 (85.1%)
    Cost: $0.1725
    Calls: 8
  gpt-4o-mini (openai)
    Tokens: 6,730 (14.9%)
    Cost: $0.0122
    Calls: 4

## Cache Efficiency
  Cache hit rate: 62.3%
  Cached tokens: 18,200
  Estimated savings: ~$0.0491

## Top Sessions by Cost
  1. claude-sonnet-4-20250514: 22,100 tokens, $0.1203
  2. claude-sonnet-4-20250514: 16,400 tokens, $0.0522
  3. gpt-4o-mini: 6,730 tokens, $0.0122
```

一眼就能看出——85% 的费用花在了 Claude Sonnet 上，缓存命中率 62%，每次缓存命中都帮我们省了 90% 的输入成本。

## 与 Hermes Agent 的差异

Hermes 的成本系统在此基础上有更多工程化考量：

1. **多层定价来源优先级**：官方快照 > 提供商 API 实时查询 > 用户自定义覆盖 > 未知（零成本）
2. **持久化统计**：使用量存入 SQLite，可以查看历史趋势（我们在之后会实现这一点）
3. **平台维度分析**：区分 CLI、Telegram、Discord 等不同渠道的使用量
4. **工具使用排行**：统计哪些工具被调用最多，帮助优化工具集

我们的实现覆盖了核心功能——规范化 Usage → 内置定价表 → 成本计算 → 多维报告。持久化和跨平台统计会在后续篇目中补充。

## 小结

本篇实现了完整的成本追踪链路：

- **Usage 规范化**：统一 Anthropic 和 OpenAI 的 5 类 token 数据
- **内置定价表**：用 `Decimal` 精确存储主流模型价格，支持模糊匹配
- **会话级累计**：每次 LLM 调用后自动记录，实时查看当前会话费用
- **分析报告**：`/insights` 命令输出总览、模型分布、缓存效率、最贵会话

下一篇我们来处理一个安全问题——Agent 执行命令和读写文件时，怎么防止敏感信息（API Key、密码）泄露到日志和 LLM 上下文中。
