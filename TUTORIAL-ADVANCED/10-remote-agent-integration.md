# 从 Coding Agent 到个人助理（十）：远程 Agent 集成

前面九篇我们构建了一个功能完善的个人助理——能上下文引用、智能路由、成本追踪、脱敏、自学技能、全文搜索、多平台接入、定时任务、甚至 RL 训练。但有一个场景它还处理不了：

> 你的同事开发了一个专门做**慢 SQL 诊断**的 Agent，另一个团队有一个**安全漏洞扫描**的 Agent。这些 Agent 各自运行在独立的服务上，你拿不到它们的代码，但你想让你的主 Agent 能**识别意图后自动调用**它们。

这就是 **Agent 互操作**问题——不同团队、不同框架、不同基础设施上的 Agent，如何发现彼此、如何通信、如何展示结果。

本篇先对比三种主流方案（MCP、A2A、HTTP API），然后分别实现 A2A 和 HTTP 两种接入方式，并用统一注册表屏蔽差异。

---

## 三种方案对比

### 方案一：MCP

MCP 是 Anthropic 主导的协议，核心思路是**把远程 Agent 包装成 Tool Server**：

```
主 Agent → MCP Client → 远程 MCP Server（包装了对方 Agent）→ 结果
```

优点：如果你已经有 MCP Client，接入零成本。
缺点：**MCP 是工具协议，不是 Agent 协议**。

| MCP 适合的 | MCP 不适合的 |
|------------|-------------|
| 查数据库、调 API、读文件 | 多轮交互（Agent 需要追问） |
| 无状态的单次调用 | 长时间运行的任务（分钟级） |
| 结果是结构化数据 | 结果是流式生成的文本或多种格式 |

MCP 的调用模型是"传参 → 拿结果"，它不支持：
- 远程 Agent 主动说"我还需要更多信息"
- 任务提交后异步执行，稍后来取结果
- 远程 Agent 返回多种格式（文本 + 文件 + 结构化数据混合）

如果对方只是一个封装好的"函数"，MCP 足够了。如果对方是一个有自主决策能力的 Agent，MCP 就不够了。

### 方案二：HTTP API（最简单）

自己定义 REST 接口：

```
POST /api/diagnose
{
  "sql": "SELECT * FROM orders WHERE ...",
  "context": "生产环境 MySQL 8.0"
}
→ {"diagnosis": "缺少索引...", "suggestion": "ALTER TABLE ..."}
```

优点：简单直接，任何语言都能对接。
缺点：

- **没有标准**——每个 Agent 的接口格式不同，主 Agent 需要为每个远程 Agent 写定制适配代码
- **没有发现机制**——你得手动配置每个 Agent 的 URL 和参数格式
- **没有状态管理**——长任务怎么办？轮询？回调？各家各做

当只有 2-3 个远程 Agent 时，HTTP API 完全够用。但如果你想构建一个**开放的 Agent 生态**——让任何人都能发布 Agent，任何主 Agent 都能自动发现和调用，就需要标准协议。

### 方案三：A2A 协议

A2A（Agent-to-Agent）由 Google 发起、现归 Linux Foundation 管理，v1.0.0 已发布。它专门解决"Agent 调用 Agent"的问题：

```
主 Agent → A2A Client → 远程 A2A Server（对方 Agent）→ 结果
```

核心特点：

| 能力 | 怎么做 |
|------|--------|
| **发现** | 远程 Agent 在 `/.well-known/agent.json` 发布 Agent Card（名称、技能、认证方式） |
| **通信** | JSON-RPC 2.0 over HTTPS，标准化的 `message/send`、`tasks/get` 等方法 |
| **多轮** | Task 有状态机（submitted → working → input-required → completed），支持追问 |
| **异步** | 长任务可以先返回 task ID，稍后轮询/SSE 流式/Webhook 推送 |
| **多格式** | 结果通过 Artifact 返回，支持文本、文件、结构化数据混合 |
| **安全** | 支持 API Key、OAuth2、mTLS 等认证方式 |

### 三方对比

| 维度 | MCP | HTTP API | A2A |
|------|-----|----------|-----|
| 定位 | 工具协议 | 自定义接口 | Agent 互操作协议 |
| 发现机制 | 配置文件中手动声明 | 无 | Agent Card（`/.well-known/agent.json`） |
| 通信协议 | JSON-RPC (stdio/SSE) | REST/gRPC | JSON-RPC / gRPC / REST |
| 多轮交互 | 不支持 | 自行实现 | Task 状态机（input-required） |
| 异步任务 | 不支持 | 自行实现 | 内建（轮询/SSE/Webhook） |
| 结果格式 | 纯文本/JSON | 自定义 | Artifact（文本+文件+数据混合） |
| 生态成熟度 | 高（数百个 Server） | — | 起步期（SDK 覆盖 5 种语言） |
| 适用场景 | 调用工具和数据源 | 少量固定 Agent | 开放 Agent 生态 |

**选型建议**：
- 对方只是一个"函数" → MCP
- 对方是完整 Agent，但只有 2-3 个 → HTTP API
- 构建开放 Agent 生态、需要标准化发现和多轮交互 → A2A

下面我们用 A2A 做端到端实现。

---

## A2A 核心概念

### Agent Card（自我介绍）

每个 A2A Agent 通过 `/.well-known/agent.json` 发布自己的能力：

```json
{
  "name": "慢 SQL 诊断助手",
  "description": "分析 SQL 语句的性能问题，给出索引建议和改写方案",
  "url": "https://sql-doctor.internal.company.com/a2a",
  "version": "1.0.0",
  "capabilities": { "streaming": true },
  "skills": [
    {
      "id": "slow-sql-diagnosis",
      "name": "慢 SQL 诊断",
      "description": "识别缺失索引、全表扫描、隐式转换等问题",
      "tags": ["sql", "performance", "mysql"]
    }
  ]
}
```

主 Agent 读到这个 Card 就知道：这个 Agent 能做什么（skills）、怎么调用（url）。Card 还可以声明认证方式（`securitySchemes`）、输入输出格式等，这里省略。

### Task 生命周期

A2A 的核心交互单位是 **Task**（任务），有明确的状态机（`submitted → working → completed/failed`），关键状态：

| 状态 | 含义 | 主 Agent 该怎么做 |
|------|------|-------------------|
| `submitted` | 任务已接收 | 等待 |
| `working` | 正在处理 | 等待（可轮询进度） |
| `input-required` | 远程 Agent 需要更多信息 | 把问题展示给用户，收集后继续发送 |
| `completed` | 完成 | 读取 artifacts 获取结果 |
| `failed` | 失败 | 展示错误信息 |

### Message 和 Part

通信的基本单位是 **Message**，包含一个或多个 **Part**：

```python
# 一条消息可以混合多种内容
message = {
    "messageId": "msg-001",
    "role": "user",          # user = 客户端发的, agent = 服务端回的
    "parts": [
        {"text": "请分析这条 SQL 的性能"},                    # 文本
        {"data": {"sql": "SELECT ...", "db": "mysql"}},       # 结构化数据
        {"url": "https://..../explain.json", "mediaType": "application/json"},  # 文件引用
    ],
}
```

### Artifact（任务产物）

远程 Agent 的**输出结果**通过 Artifact 返回，和 Message 分开：

```python
artifact = {
    "artifactId": "result-001",
    "name": "诊断报告",
    "parts": [
        {"text": "## 问题\n缺少 user_id 索引，导致全表扫描\n\n## 建议\n..."},
        {"data": {"missing_indexes": ["user_id"], "estimated_improvement": "95%"}},
    ],
}
```

为什么 Message 和 Artifact 要分开？
- **Message** 是过程中的沟通（"我在分析中..."、"请提供更多信息"）
- **Artifact** 是最终产物（诊断报告、修复脚本）

---

## 端到端实现

### 1. A2A Client

```python
import httpx
from dataclasses import dataclass, field


@dataclass
class AgentCard:
    """远程 Agent 的能力描述"""
    name: str
    url: str
    description: str = ""
    version: str = "1.0.0"
    skills: list[dict] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    security_schemes: dict = field(default_factory=dict)


class A2AClient:
    """A2A 协议客户端"""

    def __init__(self, agent_url: str, auth_token: str | None = None):
        self.agent_url = agent_url.rstrip("/")
        self.auth_token = auth_token
        self._card: AgentCard | None = None

    async def discover(self) -> AgentCard:
        """获取远程 Agent 的 Agent Card"""
        base = self.agent_url.rsplit("/", 1)[0] if "/a2a" in self.agent_url else self.agent_url
        card_url = f"{base}/.well-known/agent.json"
        async with httpx.AsyncClient() as client:
            resp = await client.get(card_url)
            resp.raise_for_status()
            data = resp.json()
        self._card = AgentCard(
            name=data["name"],
            url=data["url"],
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            skills=data.get("skills", []),
            capabilities=data.get("capabilities", {}),
            security_schemes=data.get("securitySchemes", {}),
        )
        return self._card

    async def send_message(self, text: str, task_id: str | None = None,
                           context_id: str | None = None) -> dict:
        """发送消息（阻塞模式，等待任务完成）"""
        import uuid
        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "user",
                    "parts": [{"text": text}],
                    **({"taskId": task_id} if task_id else {}),
                    **({"contextId": context_id} if context_id else {}),
                },
            },
        }
        return await self._rpc(request)

    async def get_task(self, task_id: str) -> dict:
        """查询任务状态"""
        import uuid
        request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tasks/get",
            "params": {"id": task_id},
        }
        return await self._rpc(request)

    async def _rpc(self, request: dict) -> dict:
        """发送 JSON-RPC 请求"""
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(self.agent_url, json=request, headers=headers)
            resp.raise_for_status()
            result = resp.json()
        if "error" in result:
            raise A2AError(result["error"]["message"], result["error"].get("code"))
        return result.get("result", {})


class A2AError(Exception):
    def __init__(self, message: str, code: int | None = None):
        self.code = code
        super().__init__(message)
```

### 2. 响应格式化

A2A 的响应可能是 Task（带状态机）或直接的 Message。提取两个通用的格式化函数，后续的 Tool 和多轮交互都会用到：

```python
import json


def format_parts(parts: list[dict]) -> str:
    """把 A2A 的 Part 列表格式化为文本"""
    texts = []
    for p in parts:
        if "text" in p:
            texts.append(p["text"])
        elif "data" in p:
            texts.append(
                f"```json\n{json.dumps(p['data'], ensure_ascii=False, indent=2)}\n```"
            )
        elif "url" in p:
            texts.append(f"[附件]({p['url']})")
    return "\n".join(texts)


def format_task_result(result: dict) -> str:
    """把 A2A 的 Task 或 Message 响应格式化为文本"""
    if "status" not in result:
        return format_parts(result.get("parts", []))

    task = result
    state = task["status"]["state"]

    if state == "input-required":
        msg = task["status"].get("message", {})
        return f"[远程 Agent 需要更多信息]\n{format_parts(msg.get('parts', []))}"

    if state == "failed":
        msg = task["status"].get("message", {})
        return f"[远程 Agent 执行失败]\n{format_parts(msg.get('parts', []))}"

    if state == "completed":
        artifacts = task.get("artifacts", [])
        if artifacts:
            output = []
            for a in artifacts:
                if a.get("name"):
                    output.append(f"### {a['name']}")
                output.append(format_parts(a.get("parts", [])))
            return "\n\n".join(output)
        msg = task["status"].get("message", {})
        return format_parts(msg.get("parts", []))

    return f"[任务状态: {state}]"
```

### 3. Agent Registry（远程 Agent 注册表）

管理多个远程 Agent 的发现和路由：

```python
class AgentRegistry:
    """远程 Agent 注册表"""

    def __init__(self):
        self.agents: dict[str, A2AClient] = {}
        self.cards: dict[str, AgentCard] = {}

    async def register(self, name: str, url: str, auth_token: str | None = None):
        """注册一个远程 Agent"""
        client = A2AClient(url, auth_token)
        card = await client.discover()
        self.agents[name] = client
        self.cards[name] = card

    def get_skill_descriptions(self) -> str:
        """生成所有远程 Agent 的能力描述（注入到 system prompt）"""
        lines = []
        for name, card in self.cards.items():
            lines.append(f"- **{card.name}** ({name}): {card.description}")
            for skill in card.skills:
                lines.append(f"  - {skill['name']}: {skill.get('description', '')}")
        return "\n".join(lines)
```

### 4. RemoteAgentTool（集成到主 Agent）

把 A2A 调用封装为一个 Tool，让主 Agent 的 LLM 自行决定何时调用。`schema` 定义两个参数（`agent_name` 和 `message`），格式化直接复用上面的 `format_task_result`：

```python
class RemoteAgentTool:
    name = "remote_agent"
    description = "调用远程 Agent 处理专业任务（如慢 SQL 诊断、安全扫描等）"

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    async def execute(self, input: dict) -> str:
        agent_name = input["agent_name"]
        client = self.registry.agents.get(agent_name)
        if not client:
            available = ", ".join(self.registry.agents.keys())
            return f"未找到 Agent '{agent_name}'。可用: {available}"

        result = await client.send_message(input["message"])
        return format_task_result(result)
```

### 5. System Prompt 注入

让 LLM 知道有哪些远程 Agent 可用：

```python
def build_system_prompt(registry, base_prompt: str) -> str:
    """构建包含远程 Agent 信息的 system prompt"""
    agent_info = registry.get_skill_descriptions()
    if not agent_info:
        return base_prompt

    return f"""{base_prompt}

## 可用的远程 Agent

你可以通过 remote_agent 工具调用以下远程 Agent 来处理专业任务：

{agent_info}

当用户的问题匹配某个远程 Agent 的能力时，使用 remote_agent 工具调用它。
调用时需要指定 agent_name 和要发送的 message。
"""
```

### 6. 完整接入流程

```python
import asyncio


async def main():
    # 1. 注册远程 Agent
    registry = AgentRegistry()
    await registry.register(
        "sql-doctor",
        "https://sql-doctor.internal.company.com/a2a",
        auth_token="sk-xxx",
    )
    await registry.register(
        "security-scanner",
        "https://sec-scan.internal.company.com/a2a",
        auth_token="sk-yyy",
    )

    # 2. 创建主 Agent，注入远程 Agent 工具
    agent = Agent(
        llm=llm,
        tools=[..., RemoteAgentTool(registry)],
        system_prompt=build_system_prompt(registry, BASE_PROMPT),
    )

    # 3. 用户对话——主 Agent 自动识别意图并调用远程 Agent
    agent.chat("帮我分析下这条 SQL 为什么慢：SELECT * FROM orders WHERE status = 'pending'")
    # 主 Agent 识别到 sql/performance 意图
    # → 调用 remote_agent(agent_name="sql-doctor", message="分析这条 SQL...")
    # → sql-doctor 返回诊断报告
    # → 主 Agent 整合结果回复用户
```

---

## HTTP API 方案的实现

不是所有远程 Agent 都支持 A2A。如果对方只提供了普通的 HTTP API（比如内部团队快速搭的服务），可以用一个适配层统一接入。

### HTTP Agent Client

```python
import json
from dataclasses import dataclass


@dataclass
class HttpAgentConfig:
    """HTTP Agent 的配置（手动维护，没有自动发现）"""
    name: str
    url: str
    auth_token: str | None = None
    description: str = ""
    input_mapping: dict | None = None    # 请求体模板，{message} 会被替换
    output_field: str | None = None      # 从响应中提取结果的字段路径，如 "data.result"
    timeout: int = 120


class HttpAgentClient:
    """HTTP API 方式调用远程 Agent"""

    def __init__(self, config: HttpAgentConfig):
        self.config = config

    async def call(self, message: str) -> str:
        """发送请求，返回文本结果"""
        if self.config.input_mapping:
            body = {
                k: (message if v == "{message}" else v)
                for k, v in self.config.input_mapping.items()
            }
        else:
            body = {"message": message}

        headers = {"Content-Type": "application/json"}
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(self.config.url, json=body, headers=headers)
            resp.raise_for_status()
            return self._extract_result(resp.json())

    def _extract_result(self, response: dict) -> str:
        """从响应中提取结果（支持点号路径，如 "data.result.text"）"""
        if self.config.output_field:
            obj = response
            for key in self.config.output_field.split("."):
                obj = obj[key]
            return str(obj)
        return json.dumps(response, ensure_ascii=False)
```

### 配置示例

```python
# 每个 HTTP Agent 需要手动配置——这就是没有标准协议的代价
config = HttpAgentConfig(
    name="sql-doctor",
    url="https://sql-doctor.internal.company.com/api/diagnose",
    description="分析 SQL 性能问题，给出索引建议",
    auth_token="sk-xxx",
    input_mapping={"sql": "{message}", "db_type": "mysql"},
    output_field="diagnosis",
)
```

### 统一注册表

为了让主 Agent 不关心后端是 A2A 还是 HTTP，注册表需要做一层抽象：

```python
from abc import ABC, abstractmethod


class RemoteAgent(ABC):
    """远程 Agent 的统一接口"""
    name: str
    description: str

    @abstractmethod
    async def invoke(self, message: str) -> str: ...


class A2ARemoteAgent(RemoteAgent):
    """A2A 协议的远程 Agent"""
    def __init__(self, name: str, client: A2AClient, card: AgentCard):
        self.name = name
        self.description = card.description
        self._client = client

    async def invoke(self, message: str) -> str:
        result = await self._client.send_message(message)
        return format_task_result(result)


class HttpRemoteAgent(RemoteAgent):
    """HTTP API 的远程 Agent"""
    def __init__(self, config: HttpAgentConfig):
        self.name = config.name
        self.description = config.description
        self._client = HttpAgentClient(config)

    async def invoke(self, message: str) -> str:
        return await self._client.call(message)


class UnifiedRegistry:
    """统一注册表：屏蔽 A2A / HTTP 差异"""

    def __init__(self):
        self.agents: dict[str, RemoteAgent] = {}

    async def register_a2a(self, name: str, url: str, token: str | None = None):
        client = A2AClient(url, token)
        card = await client.discover()
        self.agents[name] = A2ARemoteAgent(name, client, card)

    def register_http(self, config: HttpAgentConfig):
        self.agents[config.name] = HttpRemoteAgent(config)

    def get(self, name: str) -> RemoteAgent | None:
        return self.agents.get(name)

    def get_skill_descriptions(self) -> str:
        return "\n".join(
            f"- **{a.name}**: {a.description}" for a in self.agents.values()
        )
```

这样 `RemoteAgentTool` 只需把 `registry` 换成 `UnifiedRegistry`，`execute` 通过统一的 `invoke` 接口调用，不用关心底层协议：

```python
class RemoteAgentTool:
    def __init__(self, registry: UnifiedRegistry):
        self.registry = registry

    async def execute(self, input: dict) -> str:
        agent = self.registry.get(input["agent_name"])
        if not agent:
            return f"未找到 Agent '{input['agent_name']}'"
        return await agent.invoke(input["message"])
```

### HTTP 方案的局限

| A2A 有但 HTTP 没有的 | 影响 |
|----------------------|------|
| Agent Card 自动发现 | 每个 Agent 需要手动写配置 |
| Task 状态机 | 不支持远程 Agent 追问，只能一次性传参 |
| 异步 + SSE 流式 | 需要自行实现轮询或回调 |
| 标准化 Artifact | 每个 Agent 的响应格式不同，需要定制 `output_field` |

所以 HTTP 方案适合**快速对接少量固定 Agent**。如果远程 Agent 越来越多、交互越来越复杂，最终还是会演化到标准协议。

---

## 安全考量

### 认证

A2A 支持多种认证方式，实际使用中最常见的是 Bearer Token：

```python
# Agent Card 声明认证方式
"securitySchemes": {
    "bearer": {"type": "http", "scheme": "bearer"}
}

# 客户端在每次请求中携带
headers["Authorization"] = f"Bearer {token}"
```

### 信任边界

远程 Agent 是**不可控的**——你不知道它内部怎么工作。需要防范：

| 风险 | 防护 |
|------|------|
| 返回恶意内容（prompt injection） | 不要把远程 Agent 的原始输出直接注入 system prompt |
| 请求泄露敏感信息 | 发送前脱敏（参考[信息脱敏](04-redaction.md)） |
| 长时间不返回 | 设置超时（httpx timeout） |
| 返回超大数据 | 限制响应体大小 |

实践中，`httpx.Timeout` 限制连接和读取时间，响应体大小也应检查上限（如 10MB）。

---

## A2A vs HTTP：怎么选

这是最实际的选型问题。两种方案不是"好与坏"的关系，而是在不同约束下的最优解。

### HTTP 更好的场景

- **对接 2-3 个固定 Agent** — 写个 `HttpAgentConfig` 比部署 A2A Server 快得多
- **对方已有 REST API** — 大多数内部服务天然就是 HTTP，不需要改造
- **团队不想引入新协议** — HTTP 所有人都会，A2A 需要学习成本
- **不需要多轮交互** — 传参 → 拿结果的模式，HTTP 完全够用
- **快速验证想法** — 先用 HTTP 跑通，证明价值后再考虑标准化

### A2A 更好的场景

- **Agent 数量多且动态变化** — Agent Card 自动发现，不用为每个新 Agent 写配置
- **远程 Agent 需要追问** — `input-required` 状态让多轮交互有标准化处理方式
- **任务需要分钟级执行** — Task 状态机 + 轮询/SSE/Webhook，异步模式开箱即用
- **跨组织协作** — 标准协议意味着对方不用适配你的私有接口
- **结果格式复杂** — Artifact 支持文本 + 文件 + 结构化数据混合返回

### 对比表

| 维度 | HTTP API | A2A |
|------|----------|-----|
| 上手成本 | 几乎为零 | 需要理解协议概念 |
| 发现机制 | 手动配置 URL + 参数格式 | Agent Card 自动发现 |
| 多轮交互 | 不支持，需自行设计 | Task 状态机内建支持 |
| 异步任务 | 自行实现轮询/回调 | 轮询/SSE/Webhook 标准化 |
| 结果格式 | 每个 Agent 不同，需定制解析 | Artifact 统一格式 |
| 扩展性 | Agent 越多，适配代码越多 | 新 Agent 零适配成本 |
| 生态 | 无标准，各做各的 | 标准协议，SDK 覆盖多语言 |

### 务实建议

**从 HTTP 开始，按需演进到 A2A。** 大多数团队的起点是 2-3 个内部 Agent，HTTP 足够。当你发现自己在反复写适配代码、手动维护配置、处理各种异步回调时，就是迁移到 A2A 的信号。本篇实现的 `UnifiedRegistry` 正是为这个渐进路径设计的——先注册 HTTP Agent 跑起来，后面逐个迁移到 A2A，调用方代码不用改。

> 补充：MCP 和 A2A 解决的是不同层次的问题。MCP 是工具协议（调 API、查数据库），A2A 是 Agent 协议（调用有自主决策能力的 Agent）。两者在主 Agent 中共存，不冲突。

---

## 小结

远程 Agent 集成解决的核心问题是**Agent 互操作**——让不同团队、不同框架的 Agent 能互相发现和调用。

关键设计决策：

1. **从 HTTP 开始，按需演进** — 少量固定 Agent 用 HTTP 足够，Agent 多了再迁移到 A2A
2. **统一接口屏蔽差异** — `RemoteAgent` 抽象 + `UnifiedRegistry` 让调用方不感知底层协议
3. **Agent ≠ Tool** — 工具是无状态的函数调用，Agent 是有自主决策的实体，需要多轮交互和异步支持
4. **信任边界要清晰** — 远程 Agent 不可控，输入要脱敏，输出要验证，超时要设置
