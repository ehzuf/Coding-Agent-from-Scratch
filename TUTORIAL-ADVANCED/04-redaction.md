# 从 Coding Agent 到个人助理（四）：信息脱敏

Agent 在执行任务时会接触大量敏感信息——读取包含 API Key 的配置文件、执行 `env | grep KEY` 查看环境变量、查看 `.git/config` 里的 token。这些内容会出现在日志里、发送给 LLM API、甚至在多平台网关中被转发到 Telegram 群组。

本篇实现**信息脱敏系统**——自动识别并屏蔽日志和输出中的敏感信息。

## 威胁场景

Agent 可能泄露敏感信息的场景：

1. **日志记录**：调试日志中包含完整的 API 请求/响应
2. **工具输出**：`read` 工具读取 `.env` 文件，内容进入消息历史
3. **命令执行**：`bash` 工具执行 `cat .env` 或 `printenv`
4. **LLM 上下文**：所有工具返回的内容都会发送给 LLM API
5. **网关转发**：多平台网关会把 Agent 的回复转发到 Telegram/Discord

其中最危险的是第 4 点——敏感信息一旦进入 LLM 上下文，就可能被模型"记住"并在后续回复中输出。

## 设计思路

脱敏系统的核心是**正则模式匹配**——维护一组已知的敏感信息格式（API Key 前缀、环境变量赋值等），在文本经过关键管道时自动替换为脱敏版本。

关键设计决策：

1. **宁多勿少**：误脱敏一个普通字符串的代价远小于漏掉一个真实密钥
2. **保留可调试性**：长 token 保留首 6 尾 4 字符，开发者还能识别是哪个 Key
3. **导入时锁定**：脱敏开关在模块导入时读取，防止运行时被 LLM 生成的命令关闭

## 敏感模式库

第一层防护是识别已知格式的 API Key 和 Token：

```python
# agent/redact.py

import logging
import os
import re

# 导入时快照——防止 LLM 生成 "export REDACT_ENABLED=false" 绕过
_REDACT_ENABLED = os.getenv("AGENT_REDACT_SECRETS", "true").lower() not in (
    "0", "false", "no", "off"
)

# 已知 API Key 前缀模式
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack tokens
    r"AIza[A-Za-z0-9_-]{30,}",          # Google API keys
    r"AKIA[A-Z0-9]{16}",                # AWS Access Key ID
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe live key
    r"sk_test_[A-Za-z0-9]{10,}",        # Stripe test key
    r"SG\.[A-Za-z0-9_-]{10,}",          # SendGrid
    r"hf_[A-Za-z0-9]{10,}",             # HuggingFace
    r"npm_[A-Za-z0-9]{10,}",            # npm
    r"pypi-[A-Za-z0-9_-]{10,}",         # PyPI
    r"gsk_[A-Za-z0-9]{10,}",            # Groq Cloud
    r"pplx-[A-Za-z0-9]{10,}",           # Perplexity
]

# 编译为单一正则（性能优化）
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)
```

`(?<![A-Za-z0-9_-])` 和 `(?![A-Za-z0-9_-])` 是边界断言，确保匹配的是完整的 token 而不是某个更长字符串的子串。

把所有模式编译成一个正则的好处是**一次扫描**就能匹配所有类型，避免对同一段文本跑 15 遍正则。

## 环境变量和 JSON 字段匹配

除了已知前缀，还需要处理两种常见的敏感信息出现方式：

```python
# 环境变量赋值：OPENAI_API_KEY=sk-abc123...
_SECRET_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z0-9_]{{0,50}}{_SECRET_NAMES}[A-Z0-9_]{{0,50}})\s*=\s*(['\"]?)(\S+)\2"
)

# JSON 字段值："apiKey": "sk-abc123..."
_JSON_KEY_NAMES = r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|auth_token|bearer)"
_JSON_FIELD_RE = re.compile(
    rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)

# HTTP 认证头：Authorization: Bearer sk-abc123...
_AUTH_HEADER_RE = re.compile(
    r"(Authorization:\s*Bearer\s+)(\S+)",
    re.IGNORECASE,
)
```

`_ENV_ASSIGN_RE` 匹配的是"变量名中包含 SECRET/TOKEN/API_KEY 等关键词的赋值语句"。这样即使 Key 不以已知前缀开头（比如自定义服务的 Key），只要赋值给了名为 `MY_SERVICE_API_KEY` 的变量，就会被脱敏。

## 更多敏感模式

```python
# 私钥块
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)

# 数据库连接串密码：postgres://user:PASSWORD@host
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:]+:)([^@]+)(@)",
    re.IGNORECASE,
)

# Telegram bot token：bot123456:ABCdef...
_TELEGRAM_RE = re.compile(
    r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})"
)

# 电话号码（E.164 格式）：+8613800138000
_PHONE_RE = re.compile(r"(\+[1-9]\d{6,14})(?![A-Za-z0-9])")
```

私钥块的正则用了 `[\s\S]*?`（非贪婪匹配任意字符含换行），因为私钥内容是多行的。

## 脱敏策略

```python
def _mask_token(token):
    """脱敏一个 token
    
    短 token（< 18 字符）：完全隐藏
    长 token：保留首 6 + 尾 4 字符，便于调试
    """
    if len(token) < 18:
        return "***"
    return f"{token[:6]}...{token[-4:]}"
```

为什么保留首尾字符？

- 首 6 字符通常包含前缀（如 `sk-ant`），能帮开发者识别是哪种 Key
- 尾 4 字符能区分不同的 Key（比如团队有多个 Key 轮换使用时）
- 中间部分是真正敏感的，脱敏后无法重构出完整 Key

## 主脱敏函数

所有模式串联应用：

```python
def redact_sensitive_text(text):
    """对文本应用所有脱敏规则
    
    安全地在任何字符串上调用——不匹配的文本原样返回。
    """
    if not text or not isinstance(text, str):
        return text
    if not _REDACT_ENABLED:
        return text

    # 1. 已知前缀（sk-, ghp_, AKIA...）
    text = _PREFIX_RE.sub(lambda m: _mask_token(m.group(1)), text)

    # 2. 环境变量赋值
    def _redact_env(m):
        name, quote, value = m.group(1), m.group(2), m.group(3)
        return f"{name}={quote}{_mask_token(value)}{quote}"
    text = _ENV_ASSIGN_RE.sub(_redact_env, text)

    # 3. JSON 字段
    def _redact_json(m):
        key, value = m.group(1), m.group(2)
        return f'{key}: "{_mask_token(value)}"'
    text = _JSON_FIELD_RE.sub(_redact_json, text)

    # 4. HTTP 认证头
    text = _AUTH_HEADER_RE.sub(
        lambda m: m.group(1) + _mask_token(m.group(2)), text
    )

    # 5. 私钥块
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)

    # 6. 数据库连接串密码
    text = _DB_CONNSTR_RE.sub(
        lambda m: f"{m.group(1)}***{m.group(3)}", text
    )

    # 7. Telegram bot token
    def _redact_telegram(m):
        prefix = m.group(1) or ""
        digits = m.group(2)
        return f"{prefix}{digits}:***"
    text = _TELEGRAM_RE.sub(_redact_telegram, text)

    # 8. 电话号码
    def _redact_phone(m):
        phone = m.group(1)
        if len(phone) <= 8:
            return phone[:2] + "****" + phone[-2:]
        return phone[:4] + "****" + phone[-4:]
    text = _PHONE_RE.sub(_redact_phone, text)

    return text
```

模式的应用顺序有讲究——已知前缀先跑，把最确定的密钥格式先处理掉，避免后续模式误匹配。

## 脱敏效果

看几个例子：

```python
# API Key
redact_sensitive_text("my key is sk-ant-api03-abc123def456...")
# → "my key is sk-ant...f456..."

# 环境变量
redact_sensitive_text("OPENAI_API_KEY=sk-proj-xxxxxxxxxxxx")
# → "OPENAI_API_KEY=sk-pro...xxxx"

# JSON 配置
redact_sensitive_text('{"api_key": "ghp_1234567890abcdef"}')
# → '{"api_key": "***"}'

# 数据库连接串
redact_sensitive_text("postgres://admin:super_secret_pw@db.example.com:5432/mydb")
# → "postgres://admin:***@db.example.com:5432/mydb"

# 私钥
redact_sensitive_text("-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----")
# → "[REDACTED PRIVATE KEY]"
```

## 日志自动脱敏

Python 的 `logging` 模块支持自定义 Formatter。我们继承它，在格式化时自动脱敏：

```python
class RedactingFormatter(logging.Formatter):
    """自动脱敏的日志格式化器"""

    def format(self, record):
        original = super().format(record)
        return redact_sensitive_text(original)
```

应用到全局日志：

```python
def setup_redacting_logger():
    """配置全局日志脱敏"""
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s"
    ))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
```

配置后，所有通过 `logging` 输出的内容都会自动脱敏：

```python
import logging
logger = logging.getLogger(__name__)

# 即使日志中包含密钥，输出也是安全的
logger.info("Loaded API key: sk-ant-api03-abc123def456ghi789jkl012mno345")
# 输出: Loaded API key: sk-ant...o345
```

## 防绕过设计

注意代码最顶部的这一行：

```python
_REDACT_ENABLED = os.getenv("AGENT_REDACT_SECRETS", "true").lower() not in (
    "0", "false", "no", "off"
)
```

为什么在**导入时**读取环境变量？因为 Agent 有 `bash` 工具，能执行任意命令。如果 LLM 生成了这样的命令：

```bash
export AGENT_REDACT_SECRETS=false
cat ~/.ssh/id_rsa
```

如果脱敏开关是运行时动态检查的，第一行命令就会关闭脱敏，第二行的私钥就会完整输出。

导入时快照解决了这个问题——即使环境变量被运行时修改，脱敏模块的开关已经锁定。

## 集成到工具链

脱敏需要在关键管道中生效。以 `bash` 工具和 `read` 工具为例：

```python
# agent/tools/bash.py

from agent.redact import redact_sensitive_text

class BashTool:
    def execute(self, command):
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        
        # 脱敏命令输出
        stdout = redact_sensitive_text(result.stdout)
        stderr = redact_sensitive_text(result.stderr)
        
        return {"stdout": stdout, "stderr": stderr, "exit_code": result.returncode}
```

```python
# agent/tools/read.py

from agent.redact import redact_sensitive_text

class ReadTool:
    def execute(self, path):
        content = Path(path).read_text()
        
        # 脱敏文件内容（可选，取决于安全策略）
        # 注意：这会改变 Agent 看到的内容，可能影响代码理解
        # 更好的做法是只脱敏日志，不脱敏工具返回值
        return content
```

关于是否脱敏工具返回值，需要权衡：

- **脱敏**：更安全，但 Agent 可能无法正确理解代码
- **不脱敏**：Agent 能完整看到内容，但敏感信息会进入 LLM 上下文

Hermes 的做法是**分场景**：对外输出（日志、网关转发）必须脱敏，工具返回值保持原样。

## 与 Hermes Agent 的差异

Hermes 的脱敏系统在此基础上多了一些模式：

1. **更多前缀**：Codex token（gAAAA）、BrowserBase（bb_live_）、Fal.ai（fal_）等 40+ 种
2. **Telegram/Signal 专用**：Telegram bot token、E.164 电话号码的脱敏
3. **动态开关**：通过 `config.yaml` 的 `security.redact_secrets` 控制，但仍然在启动时锁定

我们的实现覆盖了最常见的 15+ 种密钥格式和 4 种通用模式（环境变量、JSON、HTTP 头、连接串），对于教学项目来说足够完整。

## 小结

信息脱敏系统的核心设计：

- **多层正则匹配**：已知前缀 → 环境变量赋值 → JSON 字段 → HTTP 头 → 私钥 → 连接串 → 电话号码
- **保留可调试性**：长 token 保留首 6 尾 4 字符
- **导入时锁定**：防止 LLM 生成命令绕过脱敏开关
- **日志自动过滤**：`RedactingFormatter` 继承 `logging.Formatter`
- **不到 200 行代码**：这是一个精简但有效的安全层

下一篇来实现 Hermes 最有特色的能力——技能自生成。Agent 完成复杂任务后，自动把经验沉淀为可复用的 Skill。
