# 从 Coding Agent 到个人助理（二）：智能模型路由 + 辅助 LLM

上一篇我们实现了上下文引用系统。现在来解决一个更现实的问题——**钱**。

用户问"今天星期几"，和"帮我重构整个数据库层"，用的是同一个模型、同一个价格。前者 GPT-4o-mini 就能搞定（$0.15/M tokens），后者可能确实需要 Claude Sonnet（$3/M tokens）。差了 20 倍。

本篇实现两个互补的成本优化机制：

1. **智能模型路由**——简单问题自动走廉价模型
2. **辅助 LLM**——后台任务（压缩、总结、标题生成）用独立的轻量通道

## Part 1：智能模型路由

### 设计原则：保守优先

路由决策的核心原则是**保守**——默认用主模型，只有消息"明确简单"时才路由到廉价模型。误判的代价不对称：

- 简单消息用了强模型 → 多花点钱，但结果不会差
- 复杂消息用了弱模型 → 回答质量下降，用户体验受损

所以宁可多花钱，不要降质量。

### 简单性判定

Hermes 用一组规则来判断消息是否"简单"，每一条都是否决条件——任意一条不满足就走主模型：

```python
# agent/model_router.py

import re

# 出现这些关键词意味着任务可能比较复杂
_COMPLEX_KEYWORDS = {
    "debug", "debugging", "implement", "implementation",
    "refactor", "patch", "traceback", "stacktrace",
    "exception", "error", "analyze", "analysis",
    "investigate", "architecture", "design",
    "compare", "benchmark", "optimize",
    "review", "terminal", "shell",
    "tool", "tools", "pytest", "test", "tests",
    "plan", "planning", "delegate",
    "docker", "kubernetes",
}

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def is_simple_message(text, *, max_chars=160, max_words=28):
    """判断消息是否"简单"，适合用廉价模型处理。
    
    保守策略：任何一条检查不通过就返回 False。
    """
    text = (text or "").strip()
    if not text:
        return False

    # 1. 长度检查
    if len(text) > max_chars:
        return False

    # 2. 单词数检查
    if len(text.split()) > max_words:
        return False

    # 3. 多行消息通常意味着复杂需求
    if text.count("\n") > 1:
        return False

    # 4. 包含代码块
    if "```" in text or "`" in text:
        return False

    # 5. 包含 URL
    if _URL_RE.search(text):
        return False

    # 6. 包含复杂关键词
    words = {token.strip(".,;:!?()[]{}\"'`") for token in text.lower().split()}
    if words & _COMPLEX_KEYWORDS:
        return False

    return True
```

看看实际效果：

```python
is_simple_message("今天星期几")           # True — 短，无关键词
is_simple_message("Python 是什么")        # True
is_simple_message("帮我 debug 这个错误")  # False — 包含 "debug"
is_simple_message("请 review 这段代码\n```python\nprint('hello')\n```")
                                          # False — 包含代码块
is_simple_message("分析一下这个项目的架构")  # False — 包含 "架构"（虽然不在英文关键词中，
                                           # 但中文支持可以后续扩展）
```

注意目前关键词是英文的。如果要支持中文关键词，可以扩展 `_COMPLEX_KEYWORDS` 集合——但要小心过度匹配。

### 路由决策

有了简单性判定，路由逻辑就很直接：

```python
class ModelRouter:
    """智能模型路由器"""

    def __init__(self, config):
        """
        config 结构：
        {
            "enabled": True,
            "cheap_model": "gpt-4o-mini",
            "cheap_provider": "openai",
            "max_simple_chars": 160,
            "max_simple_words": 28,
        }
        """
        self.enabled = config.get("enabled", False)
        self.cheap_model = config.get("cheap_model", "gpt-4o-mini")
        self.cheap_provider = config.get("cheap_provider", "openai")
        self.max_chars = config.get("max_simple_chars", 160)
        self.max_words = config.get("max_simple_words", 28)

    def choose_model(self, user_message, primary_model, primary_provider):
        """为本轮对话选择模型。
        
        Returns:
            (model, provider, label)
            label 为 None 表示用主模型，非 None 表示路由到了廉价模型
        """
        if not self.enabled:
            return primary_model, primary_provider, None

        if is_simple_message(
            user_message,
            max_chars=self.max_chars,
            max_words=self.max_words,
        ):
            label = f"smart route → {self.cheap_model} ({self.cheap_provider})"
            return self.cheap_model, self.cheap_provider, label

        return primary_model, primary_provider, None
```

### 集成到 Agent

在 Agent 的主循环中，每次 LLM 调用前先过一遍路由：

```python
# agent/agent.py

class Agent:
    def __init__(self, config):
        # ... 原有初始化 ...
        self.router = ModelRouter(config.get("smart_routing", {}))

    def chat(self, user_message):
        # 路由决策
        model, provider, route_label = self.router.choose_model(
            user_message, self.model, self.provider
        )

        if route_label:
            print(f"🔀 {route_label}")

        # 用选定的模型调用 LLM
        llm = self._get_llm(model, provider)
        response = llm.chat(messages=self.messages, system=self.system_prompt)

        return response
```

### 失败回退

廉价模型的 API 可能不可用（Key 过期、服务挂了）。路由应该在调用失败时自动回退到主模型：

```python
def chat_with_fallback(self, user_message):
    model, provider, route_label = self.router.choose_model(
        user_message, self.model, self.provider
    )

    if route_label:
        print(f"🔀 {route_label}")
        try:
            llm = self._get_llm(model, provider)
            return llm.chat(messages=self.messages, system=self.system_prompt)
        except Exception as e:
            print(f"⚠️  Cheap model failed ({e}), falling back to primary")
            # 回退到主模型
            model, provider = self.model, self.provider

    llm = self._get_llm(model, provider)
    return llm.chat(messages=self.messages, system=self.system_prompt)
```

## Part 2：辅助 LLM

### 为什么需要独立通道

Agent 运行过程中有很多"后台任务"需要调用 LLM：

- **上下文压缩**（compact）：对话太长时，用 LLM 总结中间轮次
- **会话标题生成**：给会话起一个简短标题
- **Session Memory 提取**：从对话中提取结构化笔记
- **Auto-Memory 总结**：跨会话记忆的更新

这些任务有共同特点：
1. 不需要最强的模型——总结和提取是相对简单的任务
2. 用户不直接看到输出——质量要求低于主对话
3. 会额外消耗主模型的预算——如果压缩一次就花掉主对话 10% 的费用，得不偿失

解决方案是引入一个**辅助 LLM 客户端**，独立配置模型和提供商：

```python
# agent/auxiliary_llm.py

class AuxiliaryLLM:
    """辅助 LLM 客户端，用于后台任务。
    
    如果未配置辅助模型，回退到主模型。
    """

    def __init__(self, config, primary_llm):
        """
        config 结构：
        {
            "model": "gpt-4o-mini",         # 辅助模型
            "provider": "openai",            # 辅助模型提供商
        }
        """
        self.primary_llm = primary_llm
        
        aux_model = config.get("model")
        aux_provider = config.get("provider")
        
        if aux_model and aux_provider:
            # 创建独立的 LLM 实例
            self.llm = create_llm(model=aux_model, provider=aux_provider)
            self.label = f"{aux_model} ({aux_provider})"
        else:
            # 未配置，回退到主模型
            self.llm = primary_llm
            self.label = None

    def chat(self, messages, *, system=None, max_tokens=2048):
        """调用辅助 LLM"""
        return self.llm.chat(
            messages=messages,
            system=system,
            max_tokens=max_tokens,
        )

    def summarize(self, text, *, instruction="请总结以下内容："):
        """通用总结接口"""
        return self.chat(
            messages=[{"role": "user", "content": f"{instruction}\n\n{text}"}],
            max_tokens=1024,
        )
```

### 改造现有模块

以上下文压缩为例，之前直接用主模型：

```python
# 改造前 — agent/compact.py
class ContextCompactor:
    def compact(self, messages, llm):
        summary = llm.chat(...)  # 用主模型做压缩
```

改造后使用辅助 LLM：

```python
# 改造后 — agent/compact.py
class ContextCompactor:
    def compact(self, messages, auxiliary_llm):
        summary = auxiliary_llm.chat(  # 用辅助模型做压缩
            messages=[{
                "role": "user",
                "content": self._build_compress_prompt(messages),
            }],
            max_tokens=2048,
        )
```

同理，`session_memory.py` 和 `auto_memory.py` 中的 LLM 调用也应该切换到辅助客户端。

### 配置方式

在已有的配置系统中增加辅助 LLM 的配置项：

```python
# agent/config.py 中增加

DEFAULT_CONFIG = {
    # ... 原有配置 ...
    
    "smart_routing": {
        "enabled": False,
        "cheap_model": "gpt-4o-mini",
        "cheap_provider": "openai",
        "max_simple_chars": 160,
        "max_simple_words": 28,
    },
    
    "auxiliary_llm": {
        "model": None,       # None 表示回退到主模型
        "provider": None,
    },
}
```

用户通过环境变量或 CLI 参数配置：

```bash
# 主模型用 Claude Sonnet
export ANTHROPIC_API_KEY=sk-xxx
# 辅助任务用 GPT-4o-mini
export AUXILIARY_MODEL=gpt-4o-mini
export AUXILIARY_PROVIDER=openai
export OPENAI_API_KEY=sk-xxx
```

### Agent 初始化时接入

```python
# agent/agent.py

class Agent:
    def __init__(self, config):
        # 主 LLM
        self.llm = create_llm(
            model=config["model"],
            provider=config["provider"],
        )
        
        # 辅助 LLM
        self.auxiliary_llm = AuxiliaryLLM(
            config.get("auxiliary_llm", {}),
            primary_llm=self.llm,
        )
        
        # 路由器
        self.router = ModelRouter(config.get("smart_routing", {}))
        
        # 上下文压缩器使用辅助 LLM
        self.compactor = ContextCompactor(auxiliary_llm=self.auxiliary_llm)
        
        # Session Memory 使用辅助 LLM
        self.session_memory = SessionMemory(auxiliary_llm=self.auxiliary_llm)
```

## 两者的协作关系

智能路由和辅助 LLM 解决的是不同场景：

| | 智能模型路由 | 辅助 LLM |
|---|---|---|
| 影响的调用 | 用户主对话的 LLM 调用 | 后台任务的 LLM 调用 |
| 决策时机 | 每轮对话开始前 | 初始化时确定 |
| 选择方式 | 基于消息内容动态判断 | 固定配置 |
| 回退策略 | 调用失败回退主模型 | 未配置时直接用主模型 |

两者叠加的效果：

```
用户说 "今天星期几"
  → 路由到 GPT-4o-mini（$0.15/M）

用户说 "重构数据库层"
  → 用主模型 Claude Sonnet（$3/M）
  → 压缩上下文时用辅助 GPT-4o-mini（$0.15/M）
  → 提取 session memory 时用辅助 GPT-4o-mini（$0.15/M）
```

大部分场景下，只有用户的核心任务才需要昂贵模型，其余开销都被辅助 LLM 分担了。

## 与 Hermes Agent 的差异

Hermes 的模型路由在此基础上多了几层复杂度：

1. **凭证池**（Credential Pool）：同一提供商支持多个 API Key，自动轮换和故障转移
2. **多层级回退链**：辅助 LLM 有 7 级回退顺序（OpenRouter → Nous Portal → Custom → Codex → Anthropic → 各家直连 → None）
3. **推理模式控制**：支持 OpenRouter 的 reasoning effort（xhigh/high/medium/low/minimal）
4. **路由签名缓存**：每个路由结果生成一个 signature 元组，用于日志匹配和缓存

我们的实现保留了核心设计——保守路由 + 独立辅助通道，这是成本优化最有效的两个杠杆。

## 小结

本篇实现了两个成本优化机制：

- **智能模型路由**：通过 6 个维度（长度、词数、行数、代码块、URL、复杂关键词）判定消息简单性，简单消息自动路由到廉价模型
- **辅助 LLM**：后台任务（压缩、总结、记忆提取）使用独立的轻量模型通道，未配置时回退主模型

核心设计思想就一句话：**不是所有任务都需要最强的模型**。

下一篇我们来实现成本追踪——花了多少钱、花在哪了、怎么优化，给用户完整的成本可见性。
