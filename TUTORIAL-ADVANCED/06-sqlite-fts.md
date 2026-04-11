# 从 Coding Agent 到个人助理（六）：会话存储升级 — SQLite + 全文搜索

前面的系列中，我们用 JSONL 文件实现了会话持久化——每个会话一个文件，断了能恢复。但有个明显的短板：**不能搜索**。

用户说"上次我们讨论过怎么配置 Nginx，帮我找出来"——JSONL 方案只能一个个文件打开遍历。10 个会话还好，100 个就不现实了。

本篇将会话存储从 JSONL 升级到 SQLite，并利用 FTS5 虚拟表实现全文搜索。

## 为什么是 SQLite

选 SQLite 而不是 PostgreSQL 或 Redis，原因很简单：

1. **零部署**：Python 标准库自带 `sqlite3`，不需要装任何东西
2. **单文件**：整个数据库就是一个文件，备份就是 `cp`
3. **性能足够**：单用户场景下，SQLite 的读写速度远超需求
4. **FTS5 内置**：全文搜索作为 SQLite 的扩展模块，Python 默认编译就带

## 数据模型

### 表设计

两张核心表 + 一个 FTS5 虚拟表：

```python
# agent/session_store.py

import sqlite3
import json
import os
from pathlib import Path
from datetime import datetime, timezone

_SCHEMA = """
-- 会话表
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    provider TEXT DEFAULT '',
    system_prompt TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    title TEXT
);

-- 消息表
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,           -- JSON 序列化的工具调用列表
    tool_name TEXT,
    timestamp TEXT NOT NULL,
    token_count INTEGER DEFAULT 0,
    finish_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

-- FTS5 全文搜索虚拟表
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    role,
    content,
    content=messages,
    content_rowid=id
);

-- 自动同步触发器
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, role, content)
    VALUES (new.id, new.role, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, role, content)
    VALUES ('delete', old.id, old.role, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, role, content)
    VALUES ('delete', old.id, old.role, old.content);
    INSERT INTO messages_fts(rowid, role, content)
    VALUES (new.id, new.role, new.content);
END;
"""
```

FTS5 虚拟表的关键配置：

- `content=messages`：告诉 FTS5 内容来自 `messages` 表（"内容表"模式）
- `content_rowid=id`：使用 messages 表的 id 作为行标识
- 三个触发器（INSERT/DELETE/UPDATE）自动维护 FTS 索引——我们不需要手动管理

## 并发控制

SQLite 默认的锁机制是"数据库级排他锁"——写操作会阻塞所有读操作。这在 Agent 场景下会出问题：主对话在写消息的同时，后台审查 Agent 可能在读消息做搜索。

解决方案是 **WAL（Write-Ahead Logging）模式**：

```python
class SessionDB:
    """SQLite 会话存储"""

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = Path.home() / ".coding-agent" / "sessions.db"
        
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,  # 允许跨线程使用
        )
        self._conn.row_factory = sqlite3.Row
        
        # 启用 WAL 模式
        self._conn.execute("PRAGMA journal_mode=WAL")
        
        # 初始化 schema
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        
        # 写入计数器，用于定期 checkpoint
        self._write_count = 0
```

WAL 模式的好处：
- **写不阻塞读**：写入操作记录到 WAL 文件，读取操作从主数据库文件读
- **并发安全**：多个线程可以同时读，一个线程写
- **自动恢复**：崩溃后 WAL 文件会在下次打开时自动回放

### 应用层写入重试

即使有 WAL 模式，并发写入仍然需要排队。如果后台线程正在写，主线程的写操作会收到 `SQLITE_BUSY`。标准做法是重试：

```python
import time
import random

def _execute_write(self, fn):
    """带重试的写操作包装器"""
    max_retries = 5
    
    for attempt in range(max_retries):
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            result = fn(self._conn)
            self._conn.commit()
            
            # 定期 WAL checkpoint
            self._write_count += 1
            if self._write_count % 50 == 0:
                self._try_wal_checkpoint()
            
            return result
        except sqlite3.OperationalError as e:
            self._conn.rollback()
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                # 随机抖动，避免多个线程同步重试
                jitter = random.uniform(0.02, 0.15)
                time.sleep(jitter)
                continue
            raise

def _try_wal_checkpoint(self):
    """尽力执行 WAL checkpoint，限制 WAL 文件增长"""
    try:
        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass  # checkpoint 失败不影响正常操作
```

重试时的**随机抖动**（20-150ms）很重要——如果多个线程以固定间隔重试，它们可能会永远撞在一起（确定性退避的"队列效应"）。随机化打破了这个模式。

`BEGIN IMMEDIATE` 而不是默认的 `BEGIN`——立即获取写锁，避免在事务中间才发现锁冲突。

## CRUD 操作

### 创建会话

```python
def create_session(self, session_id, model, provider="", system_prompt=""):
    """创建新会话"""
    now = datetime.now(timezone.utc).isoformat()
    
    def _write(conn):
        conn.execute(
            "INSERT INTO sessions (id, model, provider, system_prompt, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, model, provider, system_prompt, now),
        )
    
    self._execute_write(_write)
```

### 保存消息

```python
def save_message(self, session_id, role, content, **kwargs):
    """保存一条消息（自动更新 FTS 索引）"""
    now = datetime.now(timezone.utc).isoformat()
    tool_calls = kwargs.get("tool_calls")
    if tool_calls and not isinstance(tool_calls, str):
        tool_calls = json.dumps(tool_calls)
    
    def _write(conn):
        conn.execute(
            "INSERT INTO messages "
            "(session_id, role, content, tool_call_id, tool_calls, tool_name, "
            " timestamp, token_count, finish_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, role, content,
                kwargs.get("tool_call_id"),
                tool_calls,
                kwargs.get("tool_name"),
                now,
                kwargs.get("token_count", 0),
                kwargs.get("finish_reason"),
            ),
        )
        # 更新会话统计
        conn.execute(
            "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
            (session_id,),
        )
    
    self._execute_write(_write)
```

因为有 FTS5 的 AFTER INSERT 触发器，`save_message` 写入 messages 表时，FTS 索引**自动更新**——不需要任何额外代码。

### 加载会话

```python
def get_session(self, session_id):
    """获取会话元数据"""
    row = self._conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row else None

def get_messages(self, session_id):
    """获取会话的所有消息"""
    rows = self._conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    
    messages = []
    for row in rows:
        msg = {"role": row["role"], "content": row["content"]}
        if row["tool_call_id"]:
            msg["tool_call_id"] = row["tool_call_id"]
        if row["tool_calls"]:
            msg["tool_calls"] = json.loads(row["tool_calls"])
        if row["tool_name"]:
            msg["name"] = row["tool_name"]
        messages.append(msg)
    
    return messages
```

### 结束会话

```python
def end_session(self, session_id, reason="completed", cost_usd=0.0):
    """标记会话结束"""
    now = datetime.now(timezone.utc).isoformat()
    
    def _write(conn):
        conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ?, estimated_cost_usd = ? "
            "WHERE id = ?",
            (now, reason, cost_usd, session_id),
        )
    
    self._execute_write(_write)
```

## 全文搜索

这是升级到 SQLite 最大的收益——FTS5 提供了开箱即用的全文搜索能力。

### 搜索接口

```python
def search(self, query, limit=20):
    """全文搜索历史消息
    
    Args:
        query: 搜索关键词（支持 FTS5 查询语法）
        limit: 最大返回条数
    
    Returns:
        匹配的消息列表，按相关性排序
    """
    rows = self._conn.execute(
        """
        SELECT m.*, s.model, s.started_at as session_started,
               rank
        FROM messages_fts fts
        JOIN messages m ON m.id = fts.rowid
        JOIN sessions s ON s.id = m.session_id
        WHERE messages_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()

    results = []
    for row in rows:
        results.append({
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"][:200],  # 截取预览
            "model": row["model"],
            "session_started": row["session_started"],
            "timestamp": row["timestamp"],
        })

    return results
```

FTS5 的 `MATCH` 运算符支持丰富的查询语法：

```python
# 基本关键词搜索
db.search("nginx配置")

# 短语搜索（精确匹配）
db.search('"reverse proxy"')

# AND 组合
db.search("nginx AND https")

# 排除
db.search("nginx NOT apache")

# 前缀匹配
db.search("docker*")  # 匹配 docker、dockerfile、docker-compose...
```

### 注册 /search 命令

```python
# agent/__main__.py

while True:
    user_input = input("> ").strip()

    if user_input.startswith("/search "):
        query = user_input[8:].strip()
        results = session_db.search(query)
        if not results:
            print("No results found.")
        else:
            print(f"Found {len(results)} result(s):\n")
            for i, r in enumerate(results, 1):
                print(f"  {i}. [{r['role']}] {r['content']}")
                print(f"     Session: {r['session_id'][:8]}... | {r['session_started']}")
                print()
        continue

    # ... 正常对话 ...
```

### 搜索结果注入上下文

搜索最大的价值不只是展示给用户看，而是可以把相关历史注入当前对话的上下文：

```python
def search_and_inject(self, query, agent):
    """搜索历史并注入到当前对话上下文"""
    results = self.search(query, limit=5)
    if not results:
        return None

    context = "--- Relevant History ---\n\n"
    for r in results:
        context += f"[{r['role']}] {r['content']}\n\n"

    return context
```

这样 Agent 就能"回忆"之前的对话内容——"上次我们讨论过这个，当时是这么解决的..."

## Schema 迁移

数据库上线后，schema 可能需要演进。我们用版本号管理迁移：

```python
_CURRENT_VERSION = 2

def _init_schema(self):
    """初始化 schema 并执行迁移"""
    # 检查当前版本
    try:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception:
        version = 0

    if version < 1:
        self._conn.executescript(_SCHEMA)
        self._conn.execute("PRAGMA user_version = 1")
        self._conn.commit()

    if version < 2:
        # v2: 添加 title 字段
        try:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
        except sqlite3.OperationalError:
            pass  # 字段已存在
        self._conn.execute("PRAGMA user_version = 2")
        self._conn.commit()
```

`ALTER TABLE ADD COLUMN` 的好处是**向后兼容**——旧数据不需要迁移，新字段默认为 NULL。

## 集成到 Agent

替换原有的 JSONL 持久化：

```python
# agent/agent.py

class Agent:
    def __init__(self, config):
        # ... 原有初始化 ...
        self.session_db = SessionDB()
    
    def start_session(self):
        session_id = str(uuid.uuid4())
        self.session_db.create_session(
            session_id=session_id,
            model=self.model,
            provider=self.provider,
            system_prompt=self.system_prompt,
        )
        return session_id
    
    def _save_message(self, role, content, **kwargs):
        """每次消息交互后保存"""
        self.session_db.save_message(
            session_id=self.session_id,
            role=role,
            content=content,
            **kwargs,
        )
    
    def resume_session(self, session_id):
        """恢复历史会话"""
        session = self.session_db.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        
        self.session_id = session_id
        self.messages = self.session_db.get_messages(session_id)
        self.system_prompt = session.get("system_prompt", "")
        
        return session
```

## 与 Hermes Agent 的差异

Hermes 的 SQLite 存储（`hermes_state.py`）在此基础上有更多工程化考量：

1. **6 个版本的 Schema 迁移**：从最初版本到当前版本的完整演进链
2. **parent_session_id**：上下文压缩触发的会话分裂追踪
3. **完整的计费字段**：billing_provider、billing_mode、cost_status、cost_source、pricing_version
4. **会话列表查询**：按时间、平台、模型等多维度筛选
5. **会话恢复时加载 system_prompt**：保持 Prompt Caching 的有效性

我们的实现覆盖了核心功能——WAL 并发控制 + FTS5 全文搜索 + Schema 迁移。对于教学项目来说，这些是最有教学价值的部分。

## 小结

本篇将会话存储从 JSONL 升级到 SQLite + FTS5：

- **SQLite + WAL 模式**：单文件数据库，写不阻塞读，支持后台线程并发访问
- **应用层重试**：随机抖动 20-150ms，破坏确定性退避的队列效应
- **FTS5 全文搜索**：触发器自动同步索引，支持关键词/短语/组合查询
- **Schema 迁移**：`PRAGMA user_version` + `ALTER TABLE ADD COLUMN`，向后兼容
- **搜索注入上下文**：历史搜索结果可以直接注入当前对话

从"能恢复"到"能搜索"，记忆系统的实用性有了质的飞跃。

下一篇也是最后一篇——多平台消息网关。让 Agent 从 CLI 走向 Telegram、Discord，随时随地可用。
