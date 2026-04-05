# 从零实现 Coding Agent（十一）：Prompt Caching

在前面几步中，我们的 Agent 已经能够执行工具、管理上下文、控制权限了。但随着使用频率增加，API 费用开始变得显眼——每次请求都要把完整的 system prompt、工具定义和历史消息重新发给 LLM，其中大量内容根本没有变化。

Prompt Caching 就是解决这个问题的：让 LLM 服务提供商在服务端缓存那些重复的 token，命中缓存时只收取极低费用（Anthropic 折扣 90%，OpenAI 折扣 50%）。

---

## 什么值得缓存？

判断标准只有一个：**长、稳定、重复**。

```
每次请求都要发送的内容：
┌─────────────────────────────────────────────┐
│ system prompt（通常 500-2000 tokens）        │ ← 完全不变，最佳缓存对象
│ tools 定义（6个工具 ≈ 800 tokens）          │ ← 完全不变，自动受益
│ 历史消息 turn 1 ~ turn N-3（可能很长）      │ ← 基本不变，可以标记
│ 最近 3 条消息（最新的对话）                  │ ← 经常变，不适合缓存
└─────────────────────────────────────────────┘
```

一次典型的多轮对话，到第 10 轮时，前 7 轮的历史消息每次都原封不动地重发。如果这 7 轮包含大量工具输出（读文件、执行命令的结果），可能有几千 token 在白白计费。

---

## Anthropic vs OpenAI 的实现差异

两家服务商的实现思路完全不同：

**Anthropic：手动标记 breakpoint**

需要在你想缓存的内容最后一个 block 上加 `cache_control: {"type": "ephemeral"}`，告诉 Anthropic "请缓存到这里"。Anthropic 会把这个位置之前的所有 token 编译成一个缓存快照。

```python
# Anthropic 要求：cache_control 只能加在 content block 上，不能是纯字符串
system = [
    {
        "type": "text",
        "text": "你是一个 coding agent...",
        "cache_control": {"type": "ephemeral"},  # ← 这里打标记
    }
]
```

**OpenAI：全自动，无需标记**

OpenAI 自动缓存相同前缀的 token，开发者无需任何代码改动。命中缓存的 token 数量体现在 `usage.prompt_tokens_details.cached_tokens` 里。

这个差异直接影响了我们的实现策略：Anthropic 需要主动"打标记"，OpenAI 只需"读结果"。

---

## 核心实现

### 1. 给 system prompt 打标记

```python
# agent/llm/anthropic_llm.py

_CACHE_CONTROL = {"type": "ephemeral"}

def _add_cache_control_to_system(system: str) -> list[dict]:
    """
    将 system 字符串转换为带 cache_control 的 block 格式。
    
    Anthropic API 的 system 参数既接受字符串，也接受 content blocks 列表。
    要添加 cache_control，必须使用 blocks 格式。
    """
    return [
        {
            "type": "text",
            "text": system,
            "cache_control": _CACHE_CONTROL,
        }
    ]
```

这个改动很小，但效果显著——每次请求都会命中 system prompt 的缓存，节省几百到几千 token 的费用。

### 2. 给历史消息打标记

```python
def _add_cache_control_to_messages(messages: list[dict]) -> list[dict]:
    """
    在历史消息上打 cache_control 标记。
    
    策略：
    - 在最后一条消息上打标记（无论角色是什么）
      Claude Code 采用相同策略：直接在 messages[messages.length - 1] 上打标记，
      不搜索特定角色，确保尽可能多的内容被缓存
    - 只打 1 个 breakpoint（加上 system 共 2 个，最多 4 个，留余量给 tools）
    """
    if not messages:
        return messages

    cache_idx = len(messages) - 1

    result = list(messages)  # 浅拷贝，不修改原始列表
    msg = result[cache_idx]
    content = msg.get("content")

    if isinstance(content, str):
        # 字符串 content → 转为 block 格式
        new_content = [{"type": "text", "text": content, "cache_control": _CACHE_CONTROL}]
        result[cache_idx] = {**msg, "content": new_content}
    elif isinstance(content, list) and content:
        # 已经是 block 格式 → 给最后一个 block 加标记
        new_blocks = list(content)
        last_block = dict(new_blocks[-1])
        last_block["cache_control"] = _CACHE_CONTROL
        new_blocks[-1] = last_block
        result[cache_idx] = {**msg, "content": new_blocks}

    return result
```

这里有个关键细节：**浅拷贝**。我们只复制了列表，没有深拷贝每条消息。对于不需要修改的消息（索引不是 `cache_idx`），直接复用引用；只有需要修改的那条消息，才用 `{**msg, "content": new_content}` 创建新字典。这避免了对 `agent.messages` 原始数据的意外修改。

### 3. 在 _build_kwargs 中组装

```python
def _build_kwargs(self, messages, system, max_tokens, tools) -> dict:
    if self.enable_cache:
        messages = _add_cache_control_to_messages(messages)  # 历史消息标记
    
    kwargs = dict(model=self.model, max_tokens=max_tokens, messages=messages, ...)
    
    if system:
        if self.enable_cache:
            kwargs["system"] = _add_cache_control_to_system(system)  # system 标记
        else:
            kwargs["system"] = system
    
    return kwargs
```

### 4. 读取 cache 用量

```python
# Anthropic：从 response.usage 读取
def chat(self, ...) -> LLMResponse:
    response = self._client.messages.create(...)
    
    usage = response.usage
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    
    return LLMResponse(
        ...,
        cache_write_tokens=cache_write,
        cache_read_tokens=cache_read,
    )
```

```python
# OpenAI：从 usage.prompt_tokens_details 读取
def _get_cached_tokens(usage) -> int:
    try:
        details = usage.prompt_tokens_details
        if details is None:
            return 0
        return getattr(details, "cached_tokens", 0) or 0
    except Exception:
        return 0
```

用 `getattr(..., 0)` 而不是直接访问属性，是因为不同模型和服务商对这些字段的支持不同，避免 AttributeError。

---

## LLMResponse 新增字段

```python
# agent/llm/base.py
@dataclass
class LLMResponse:
    content: list[dict[str, Any]]
    input_tokens: int       # 未命中缓存的正常输入 token（正常计费）
    output_tokens: int
    model: str
    stop_reason: str | None = None
    cache_read_tokens: int = 0   # 命中缓存的 token（折扣计费）
    cache_write_tokens: int = 0  # 首次写入缓存的 token（略贵）
```

这样上层代码可以监控 cache 效果：

```python
response = agent.chat("分析这个项目的代码结构")
print(f"正常输入: {response.input_tokens}")
print(f"缓存命中: {response.cache_read_tokens}")   # 越大越省钱
print(f"缓存写入: {response.cache_write_tokens}")  # 首次才有
print(f"输出:    {response.output_tokens}")
```

---

## Anthropic Cache 的工作原理

理解工作原理有助于做出更好的决策。

Anthropic 的 cache 本质是把一段 token 序列编译成一个"快照"存在服务端（TTL 5 分钟，读取时重置计时）。

```
第 1 次请求：
  [system + tools + history(1-7) + latest(8-10)]
           ↑ cache_control 在 history 末尾
  → Anthropic 编译并存储 [system + tools + history(1-7)]
  → 计费：cache_write（略贵） + latest(8-10)（正常）

第 2 次请求（5 分钟内）：
  [system + tools + history(1-8) + latest(9-11)]
           ↑ cache_control 在 history 末尾（现在是第 8 轮末）
  → 前面已缓存的部分命中，只需计算新增内容
  → 计费：cache_read（便宜 90%） + latest(9-11)（正常）
```

**注意**：只有当请求的前缀与缓存的快照**完全相同**时才命中。如果 system prompt 稍有变化，整个缓存就会失效。这就是为什么 system prompt 是最值得缓存的——它几乎永远不变。

---

## 实际效果验证

```bash
# 第一次请求：会有 cache_write（建立缓存）
python -m agent --no-stream "列出项目结构"

# 输出类似：
# --- token 用量: 输入 320, 输出 145, 缓存写入 1840 ---

# 流式模式也能看到（不限于 --no-stream）
python -m agent "读取 agent/agent.py 的内容"

# 输出类似：
# --- token 用量: 输入 95, 输出 280, 缓存命中 1840 ---
# 节省了 1840 个 token 的费用（按 Anthropic 90% 折扣）
```

---

## 缓存分析命令

缓存这种东西，看不到就容易"信仰编程"——加了标记但不知道到底缓存了什么，命中了多少。为此我们提供了两种方式查看缓存状态。

### --show-cache（单次模式）

```bash
python -m agent --show-cache "介绍一下项目"
```

请求结束后会自动显示缓存分析。

### /cache（交互模式）

```bash
python -m agent
[1] 你: 你好
助手: 你好！有什么可以帮助你的？
[2] 你: 请读取 agent/agent.py
助手: [read] agent.py 的内容如下...
[3] 你: /cache
```

输出类似：

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

从这里可以看到：
1. **缓存的组成**：system prompt + tools 定义 + 完整的历史对话轮次
2. **cache_control 标记位置**：最后一条消息，确保尽可能多的内容被缓存
3. **具体费用节省**：已缓存的 token 下次请求只付 10% 费用

如果用的是 OpenAI 后端，显示会不同——没有 breakpoint 标记（因为是自动缓存），但会显示预估可缓存的总量。

### 实现细节

`show_cache_info` 函数会检查 `agent._is_anthropic` 来区分后端，因为两家的缓存机制完全不同：

```python
def show_cache_info(agent: Agent):
    is_anthropic = agent._is_anthropic

    if is_anthropic:
        # 直接在最后一条消息上打 cache_control（与 Claude Code 一致）
        cache_idx = len(messages) - 1
        # 计算 system + tools + 历史前部分的总缓存量
    else:
        # OpenAI 自动缓存，显示预估可缓存总量
```

---

## 一个容易踩的坑：content 格式

Anthropic API 的 messages 中，`content` 字段有两种格式：

```python
# 格式一：纯字符串（简单，但无法加 cache_control）
{"role": "user", "content": "你好"}

# 格式二：content blocks 列表（可以加 cache_control）
{"role": "user", "content": [{"type": "text", "text": "你好"}]}
```

`cache_control` 只能加在 content block（格式二）上。因此在 `_add_cache_control_to_messages` 里，需要检测 content 类型并做必要转换：

```python
if isinstance(content, str):
    # 需要先转换为 block 格式
    new_content = [{"type": "text", "text": content, "cache_control": _CACHE_CONTROL}]
elif isinstance(content, list) and content:
    # 已经是 block 格式，直接给最后一个 block 加标记
    ...
```

工具调用相关的消息（`tool_use` / `tool_result`）本来就是 block 格式，不需要转换，会自动走 `isinstance(content, list)` 分支。

---

## 小结

Prompt Caching 的实现思路很简单：**找到不变的内容，告诉服务商缓存它**。

| | Anthropic | OpenAI |
|---|---|---|
| 实现方式 | 手动加 `cache_control` 标记 | 自动缓存相同前缀 |
| 最佳缓存对象 | system prompt（必须转为 block 格式）| 相同前缀的 token |
| 命中折扣 | 90% 折扣（仅付 10%）| 50% 折扣 |
| 写入成本 | 首次略贵（1.25x）| 无额外成本 |
| TTL | 5 分钟（读取时重置）| 数分钟（具体不公开）|

对于一个有丰富工具定义和较长 system prompt 的 Agent，prompt caching 通常能节省 **50~80%** 的输入 token 费用。实现成本极低，收益显著。

下一篇将实现**并发工具执行**——当 LLM 在一次响应中请求多个无依赖关系的工具时，并发执行而非串行，大幅降低延迟。
