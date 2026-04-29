# 从 Coding Agent 到个人助理（七）：多平台消息网关

前面六篇我们完成了一个功能丰富的个人助理——上下文引用、智能路由、成本追踪、信息脱敏、技能自生成、全文搜索。但它只有一个入口：命令行。

如果你想通过 Telegram 发消息给 Agent、同时在 Slack 群里也能用、甚至接收邮件自动处理，就需要一个**多平台消息网关**。本篇实现网关架构的核心——平台适配器模式。

## 网关架构总览

多平台网关的核心思路是**消息归一化**：不管消息来自 Telegram、Discord 还是 Slack，统一转换为内部标准格式，交给同一个 Agent 处理，再把响应路由回原始平台。

```
Telegram ──→ TelegramAdapter ──→ MessageEvent ──→ GatewayRunner ──→ Agent
Discord  ──→ DiscordAdapter  ──→ MessageEvent ──↗                    │
Slack    ──→ SlackAdapter    ──→ MessageEvent ──↗                    │
                                                                      │
Telegram ←── TelegramAdapter ←── response text ←── GatewayRunner ←──┘
```

关键设计决策：

1. **适配器模式**：每个平台实现统一的 `BasePlatformAdapter` 接口
2. **消息归一化**：所有平台的消息转换为 `MessageEvent`，响应转换为 `SendResult`
3. **会话隔离**：通过 `SessionSource` + 确定性 session key 实现多用户/多群组/多线程隔离
4. **中断支持**：同一会话内的新消息可以中断正在执行的 Agent
5. **自动重连**：平台断线后指数退避重连，不影响其他平台

## 统一消息模型

网关的基础是两个数据结构：`MessageEvent`（入站）和 `SendResult`（出站）。

```python
# gateway/models.py

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, List
from datetime import datetime


class MessageType(Enum):
    """入站消息类型"""
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    COMMAND = "command"  # /command 风格


class Platform(Enum):
    """支持的平台"""
    LOCAL = "local"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    # 按需扩展...


@dataclass
class SessionSource:
    """消息来源描述——用于会话路由和上下文注入"""
    platform: Platform
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"  # "dm", "group", "channel"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    thread_id: Optional[str] = None  # 论坛主题、Discord 线程等

    @property
    def description(self) -> str:
        if self.platform == Platform.LOCAL:
            return "CLI terminal"
        if self.chat_type == "dm":
            return f"DM with {self.user_name or self.user_id or 'user'}"
        return f"{self.chat_type}: {self.chat_name or self.chat_id}"


@dataclass
class MessageEvent:
    """
    归一化的入站消息。
    
    所有平台适配器产出这个统一结构，GatewayRunner 只处理这一种类型。
    """
    text: str
    message_type: MessageType = MessageType.TEXT
    source: Optional[SessionSource] = None
    raw_message: Any = None  # 平台原始消息对象，适配器内部用
    message_id: Optional[str] = None

    # 媒体附件
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)

    # 回复上下文
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None

    timestamp: datetime = field(default_factory=datetime.now)

    def is_command(self) -> bool:
        return self.text.startswith("/")

    def get_command(self) -> Optional[str]:
        if not self.is_command():
            return None
        parts = self.text.split(maxsplit=1)
        return parts[0][1:].lower() if parts else None

    def get_command_args(self) -> str:
        if not self.is_command():
            return self.text
        parts = self.text.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""


@dataclass
class SendResult:
    """发送结果——适配器返回此结构表示投递状态"""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False  # True 表示是网络抖动等暂时性错误，可以重试
```

`SessionSource` 是会话路由的关键。同一个用户在不同平台、不同群组的消息会被隔离到不同会话中：

```python
def build_session_key(
    source: SessionSource,
    group_sessions_per_user: bool = True,
) -> str:
    """
    从消息来源构造确定性的 session key。
    
    规则：
    - DM: platform:dm:chat_id[:thread_id]
    - Group: platform:group:chat_id[:user_id]（按需隔离到个人）
    """
    platform = source.platform.value

    if source.chat_type == "dm":
        key = f"agent:{platform}:dm:{source.chat_id}"
        if source.thread_id:
            key += f":{source.thread_id}"
        return key

    # 群组/频道
    parts = ["agent", platform, source.chat_type]
    if source.chat_id:
        parts.append(source.chat_id)
    if source.thread_id:
        parts.append(source.thread_id)
    if group_sessions_per_user and source.user_id:
        parts.append(source.user_id)
    return ":".join(parts)
```

为什么需要确定性 key？因为同一用户的多条消息必须路由到同一个 Agent 实例。session key 是这个路由的唯一标识。

## 平台适配器基类

所有平台适配器继承 `BasePlatformAdapter`，实现统一的生命周期和消息处理接口：

```python
# gateway/base_adapter.py

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Dict, Optional

from gateway.models import (
    MessageEvent, MessageType, SendResult, SessionSource,
    Platform, build_session_key,
)

logger = logging.getLogger(__name__)

# 消息处理器类型：接收 MessageEvent，返回可选的响应文本
MessageHandler = Callable[[MessageEvent], Awaitable[Optional[str]]]


@dataclass
class PlatformConfig:
    """单个平台的配置"""
    enabled: bool = False
    token: Optional[str] = None
    api_key: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class BasePlatformAdapter(ABC):
    """
    平台适配器基类。
    
    子类实现平台特定的：连接/认证、接收消息、发送消息、媒体处理。
    基类提供通用的：消息处理编排、中断支持、重试、错误恢复。
    """

    def __init__(self, config: PlatformConfig, platform: Platform):
        self.config = config
        self.platform = platform
        self._message_handler: Optional[MessageHandler] = None
        self._running = False

        # 致命错误追踪
        self._fatal_error_message: Optional[str] = None
        self._fatal_error_retryable = True
        self._fatal_error_handler = None

        # 会话活跃状态追踪（用于中断支持）
        self._active_sessions: Dict[str, asyncio.Event] = {}
        self._pending_messages: Dict[str, MessageEvent] = {}
        self._background_tasks: set[asyncio.Task] = set()

    # ── 抽象方法：子类必须实现 ─────────────────────────
    @abstractmethod
    async def connect(self) -> bool:
        """连接到平台。成功返回 True。"""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接，清理资源。"""
        ...

    @abstractmethod
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SendResult:
        """发送文本消息到指定聊天。"""
        ...

    # ── 可选方法：子类按需覆写 ─────────────────────────
    async def send_image(self, chat_id: str, image_url: str, **kwargs) -> SendResult:
        return SendResult(success=False, error="Not supported")

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        pass  # 默认无操作

    # ── 通用属性 ────────────────────────────────────────
    @property
    def name(self) -> str:
        return self.platform.value.title()

    @property
    def is_connected(self) -> bool:
        return self._running

    @property
    def has_fatal_error(self) -> bool:
        return self._fatal_error_message is not None

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._message_handler = handler

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._running = False
        self._fatal_error_message = message
        self._fatal_error_retryable = retryable

    # ── 消息处理编排 ────────────────────────────────────
    async def handle_message(self, event: MessageEvent) -> None:
        """
        入站消息的统一入口。
        
        立即返回，消息处理在后台任务中进行。
        同会话消息顺序处理：新消息到达时，若该会话已有任务在跑，
        新消息排队，等当前任务结束后再处理（本教程不实现运行时中断）。
        """
        if not self._message_handler:
            return

        session_key = build_session_key(event.source)

        # 会话已有活跃任务——触发中断
        if session_key in self._active_sessions:
            # 紧急命令（/stop, /new）直接旁路
            cmd = event.get_command()
            if cmd in ("stop", "new", "reset"):
                try:
                    response = await self._message_handler(event)
                    if response:
                        await self.send(chat_id=event.source.chat_id, content=response)
                except Exception as e:
                    logger.error("[%s] Command bypass failed: %s", self.name, e)
                return

            # 常规消息排队；保留 Event 作为未来实现运行时中断的钩子
            self._pending_messages[session_key] = event
            self._active_sessions[session_key].set()
            return

        # 标记会话活跃（在 spawn 之前，关闭竞态窗口）
        self._active_sessions[session_key] = asyncio.Event()
        task = asyncio.create_task(self._process_message(event, session_key))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _process_message(self, event: MessageEvent, session_key: str) -> None:
        """后台消息处理——带 typing 指示和错误恢复"""
        typing_task = asyncio.create_task(self._keep_typing(event.source.chat_id))
        try:
            response = await self._message_handler(event)
            if response:
                await self._send_with_retry(
                    chat_id=event.source.chat_id,
                    content=response,
                    reply_to=event.message_id,
                )

            # 检查中断期间是否有排队消息
            if session_key in self._pending_messages:
                pending = self._pending_messages.pop(session_key)
                del self._active_sessions[session_key]
                typing_task.cancel()
                await self._process_message(pending, session_key)
                return

        except Exception as e:
            logger.error("[%s] Error: %s", self.name, e, exc_info=True)
            try:
                await self.send(
                    chat_id=event.source.chat_id,
                    content=f"Sorry, an error occurred: {type(e).__name__}. Try /reset.",
                )
            except Exception:
                pass
        finally:
            typing_task.cancel()
            self._active_sessions.pop(session_key, None)

    async def _keep_typing(self, chat_id: str) -> None:
        """持续刷新 typing 指示器，直到任务完成"""
        try:
            while True:
                await self.send_typing(chat_id=chat_id)
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        max_retries: int = 3,
    ) -> SendResult:
        """带指数退避的重试发送"""
        for attempt in range(max_retries):
            result = await self.send(
                chat_id=chat_id, content=content, reply_to=reply_to,
            )
            if result.success or not result.retryable:
                return result
            delay = min(2 ** attempt, 10)
            logger.warning(
                "[%s] Send failed (attempt %d/%d), retrying in %ds: %s",
                self.name, attempt + 1, max_retries, delay, result.error,
            )
            await asyncio.sleep(delay)
        return result

    def build_source(
        self,
        chat_id: str,
        chat_type: str = "dm",
        user_id: Optional[str] = None,
        user_name: Optional[str] = None,
        thread_id: Optional[str] = None,
        chat_name: Optional[str] = None,
    ) -> SessionSource:
        """辅助方法：构造 SessionSource"""
        return SessionSource(
            platform=self.platform,
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(user_id) if user_id else None,
            user_name=user_name,
            thread_id=str(thread_id) if thread_id else None,
        )

    def cancel_background_tasks(self) -> None:
        """关闭时取消所有后台任务"""
        for task in self._background_tasks:
            task.cancel()
```

基类做了几件重要的事：

1. **串行编排**：`handle_message()` 检查会话是否已有任务在跑。如果有，新消息排队等当前任务结束后再处理（本教程不中断运行中的 Agent，中断机制留作练习）。紧急命令（`/stop`、`/new`）绕过排队直接执行——否则会被当成普通消息吞掉。

2. **竞态防护**：在 `asyncio.create_task` 之前就标记 `_active_sessions`，这样第二条消息到达时不会因为任务还没开始而误判为"没有活跃任务"。

3. **排队消息续处理**：`_process_message` 完成后检查有没有排队的 pending 消息，有则继续处理，实现无缝衔接。

4. **重试发送**：暂时性错误（网络抖动、429 限流）用指数退避重试，非暂时性错误直接返回。

## Telegram 适配器实现

以 Telegram 为例实现一个具体的适配器。核心任务：用 `python-telegram-bot` 库对接 Bot API，把 Telegram 的更新事件转换为 `MessageEvent`。

```python
# gateway/adapters/telegram.py

import os
import asyncio
import logging
from typing import Optional, Dict, Any

from gateway.base_adapter import BasePlatformAdapter, PlatformConfig
from gateway.models import (
    Platform, MessageEvent, MessageType, SendResult,
)

logger = logging.getLogger(__name__)

try:
    from telegram import Update, Bot
    from telegram.ext import Application, MessageHandler as TGHandler, filters
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False


class TelegramAdapter(BasePlatformAdapter):
    """Telegram Bot 适配器"""

    MAX_MESSAGE_LENGTH = 4096

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.TELEGRAM)
        self._app: Optional[Application] = None
        self._bot: Optional[Bot] = None
        # 媒体批次合并：快速连发的照片合并为单条消息
        self._media_batch_delay = float(
            os.getenv("TELEGRAM_MEDIA_BATCH_DELAY", "0.8")
        )
        self._pending_batches: Dict[str, MessageEvent] = {}
        self._batch_tasks: Dict[str, asyncio.Task] = {}

    async def connect(self) -> bool:
        if not TELEGRAM_AVAILABLE:
            logger.error("[Telegram] python-telegram-bot not installed")
            return False
        if not self.config.token:
            logger.error("[Telegram] No bot token configured")
            return False

        try:
            self._app = (
                Application.builder()
                .token(self.config.token)
                .build()
            )
            self._bot = self._app.bot

            # 注册消息处理器
            self._app.add_handler(
                TGHandler(filters.ALL, self._on_message)
            )

            # 启动轮询（非阻塞）
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )

            self._running = True
            logger.info("[Telegram] Connected via polling")
            return True

        except Exception as e:
            error_msg = str(e)
            # 区分可重试和不可重试的错误
            if "Unauthorized" in error_msg or "token" in error_msg.lower():
                self._set_fatal_error("auth_failed", error_msg, retryable=False)
            else:
                self._set_fatal_error("connect_failed", error_msg, retryable=True)
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._app:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning("[Telegram] Disconnect error: %s", e)

    async def _on_message(self, update: Update, context: Any) -> None:
        """Telegram 消息回调——转换为 MessageEvent 并交给基类处理"""
        message = update.effective_message
        if not message:
            return
        chat = update.effective_chat
        user = update.effective_user

        # 判断消息类型
        if message.photo:
            msg_type = MessageType.PHOTO
            text = message.caption or ""
        elif message.voice or message.audio:
            msg_type = MessageType.VOICE
            text = message.caption or ""
        elif message.document:
            msg_type = MessageType.DOCUMENT
            text = message.caption or ""
        else:
            msg_type = MessageType.TEXT
            text = message.text or ""

        if not text and msg_type == MessageType.TEXT:
            return  # 忽略空消息

        # 判断聊天类型
        if chat.type == "private":
            chat_type = "dm"
        elif chat.type in ("group", "supergroup"):
            chat_type = "group"
        else:
            chat_type = "channel"

        # 构造归一化的 MessageEvent
        source = self.build_source(
            chat_id=str(chat.id),
            chat_type=chat_type,
            user_id=str(user.id) if user else None,
            user_name=user.username if user else None,
            thread_id=str(message.message_thread_id) if message.message_thread_id else None,
            chat_name=chat.title or (user.username if user else None),
        )

        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            raw_message=update,
            message_id=str(message.message_id),
        )

        # 照片批次合并：快速连发的照片延迟处理
        if msg_type == MessageType.PHOTO and message.photo:
            await self._batch_photo(event, source.chat_id)
            return

        await self.handle_message(event)

    async def _batch_photo(self, event: MessageEvent, batch_key: str) -> None:
        """
        合并快速连发的照片。
        
        Telegram 客户端发送多张照片时，会在 0.5~1 秒内连续发多条消息。
        我们等待一小段时间，把它们合并为一个 MessageEvent，避免每张照片
        都触发一轮 Agent 处理。
        """
        if batch_key in self._pending_batches:
            existing = self._pending_batches[batch_key]
            existing.media_urls.extend(event.media_urls)
            if event.text:
                existing.text = f"{existing.text}\n{event.text}".strip()
        else:
            self._pending_batches[batch_key] = event

        # 取消之前的定时器，重新开始计时
        if batch_key in self._batch_tasks:
            self._batch_tasks[batch_key].cancel()

        async def _flush():
            await asyncio.sleep(self._media_batch_delay)
            if batch_key in self._pending_batches:
                batched = self._pending_batches.pop(batch_key)
                self._batch_tasks.pop(batch_key, None)
                await self.handle_message(batched)

        self._batch_tasks[batch_key] = asyncio.create_task(_flush())

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> SendResult:
        if not self._bot:
            return SendResult(success=False, error="Bot not connected")

        try:
            # Telegram 消息长度限制 4096 字符
            if len(content) > self.MAX_MESSAGE_LENGTH:
                content = content[: self.MAX_MESSAGE_LENGTH - 20] + "\n\n... (truncated)"

            kwargs: dict = {
                "chat_id": int(chat_id),
                "text": content,
                "parse_mode": "Markdown",
            }
            if reply_to:
                kwargs["reply_to_message_id"] = int(reply_to)
            if metadata and metadata.get("thread_id"):
                kwargs["message_thread_id"] = int(metadata["thread_id"])

            msg = await self._bot.send_message(**kwargs)
            return SendResult(success=True, message_id=str(msg.message_id))

        except Exception as e:
            error_str = str(e)
            retryable = any(
                k in error_str.lower()
                for k in ("timeout", "connection", "network", "429", "flood")
            )
            return SendResult(success=False, error=error_str, retryable=retryable)

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        if self._bot:
            try:
                tg_kwargs: dict = {"chat_id": int(chat_id), "action": "typing"}
                thread_id = kwargs.get("metadata", {}).get("thread_id") if kwargs.get("metadata") else None
                if thread_id:
                    tg_kwargs["message_thread_id"] = int(thread_id)
                await self._bot.send_chat_action(**tg_kwargs)
            except Exception:
                pass
```

实现要点：

1. **消息类型映射**：Telegram 的 `Update` 对象可能包含 text、photo、voice、document 等，统一映射到 `MessageType` 枚举。

2. **聊天类型判断**：private → dm，group/supergroup → group，channel → channel。这决定了 session key 的构造方式。

3. **照片批次合并**：Telegram 用户发送多张照片时，客户端会在短时间内连续发送多条消息。适配器用一个延迟定时器把它们合并为单条 `MessageEvent`，避免每张照片都触发一轮 Agent 处理。这是个容易忽略但用户体验影响很大的细节。

4. **发送时的 Markdown 降级**：Telegram 的 Markdown 解析比较严格，如果格式错误整条消息都会发送失败。生产环境中需要加一层回退：先尝试 Markdown 发送，失败后用纯文本重试。

5. **错误分类**：`retryable` 标志区分暂时性错误（网络超时、429 限流）和永久性错误（token 无效），基类据此决定是否重试。

## 网关运行器

`GatewayRunner` 是整个网关的协调者——管理所有适配器的生命周期，路由消息到 Agent，处理断线重连。

```python
# gateway/runner.py

import asyncio
import time
import logging
from typing import Dict, Optional, Any

from gateway.models import Platform, MessageEvent, SessionSource, build_session_key
from gateway.base_adapter import BasePlatformAdapter, PlatformConfig

logger = logging.getLogger(__name__)


class GatewayRunner:
    """
    网关控制器：管理所有平台适配器的生命周期，路由消息到 Agent。
    """

    def __init__(self, config: dict):
        self.config = config
        self.adapters: Dict[Platform, BasePlatformAdapter] = {}
        self._running = False

        # Agent 缓存：每个 session key 复用同一个 Agent 实例
        # 保持 prompt cache，避免每条消息都重建系统提示
        self._agent_cache: Dict[str, Any] = {}

        # 断线重连队列
        self._failed_platforms: Dict[Platform, dict] = {}

    async def start(self) -> bool:
        """启动网关，连接所有已配置的平台"""
        logger.info("Starting gateway...")
        connected = 0

        platform_configs = self.config.get("platforms", {})
        for platform_name, platform_config in platform_configs.items():
            if not platform_config.get("enabled"):
                continue

            platform = Platform(platform_name)
            adapter = self._create_adapter(platform, platform_config)
            if not adapter:
                continue

            adapter.set_message_handler(self._handle_message)

            try:
                success = await adapter.connect()
                if success:
                    self.adapters[platform] = adapter
                    connected += 1
                    logger.info("✓ %s connected", platform.value)
                else:
                    logger.warning("✗ %s failed", platform.value)
                    # 可重试的错误加入重连队列
                    if adapter.has_fatal_error and adapter._fatal_error_retryable:
                        self._failed_platforms[platform] = {
                            "config": platform_config,
                            "attempts": 0,
                            "next_retry": time.monotonic() + 30,
                        }
            except Exception as e:
                logger.error("✗ %s error: %s", platform.value, e)
                self._failed_platforms[platform] = {
                    "config": platform_config,
                    "attempts": 0,
                    "next_retry": time.monotonic() + 30,
                }

        if connected == 0 and not self._failed_platforms:
            logger.error("No platforms connected")
            return False

        self._running = True
        # 启动后台任务
        asyncio.create_task(self._reconnect_watcher())
        logger.info("Gateway started with %d platform(s)", connected)
        return True

    def _create_adapter(
        self, platform: Platform, config: dict
    ) -> Optional[BasePlatformAdapter]:
        """适配器工厂——根据平台类型动态创建"""
        if platform == Platform.TELEGRAM:
            from gateway.adapters.telegram import TelegramAdapter
            return TelegramAdapter(PlatformConfig(
                enabled=True,
                token=config.get("token"),
                extra=config.get("extra", {}),
            ))
        # elif platform == Platform.DISCORD:
        #     from gateway.adapters.discord import DiscordAdapter
        #     return DiscordAdapter(...)
        # 按需扩展...
        logger.warning("No adapter for %s", platform.value)
        return None

    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        """
        核心消息处理器——所有平台的消息都汇聚到这里。
        
        职责：
        1. 解析 session key
        2. 获取或创建 Agent 实例
        3. 调用 Agent 处理消息
        4. 返回响应文本
        """
        session_key = build_session_key(event.source)

        # 处理命令
        if event.is_command():
            cmd = event.get_command()
            if cmd in ("new", "reset"):
                self._agent_cache.pop(session_key, None)
                return "Session reset. Starting fresh."
            if cmd == "stop":
                # Agent 缓存中的实例可以设置 interrupt flag
                agent = self._agent_cache.get(session_key)
                if agent and hasattr(agent, "interrupt"):
                    agent.interrupt()
                return "Stopping current task."

        # 获取或创建 Agent 实例
        agent = self._get_or_create_agent(session_key, event.source)

        # 调用 Agent
        try:
            response = await asyncio.to_thread(
                agent.chat, event.text
            )
            return response
        except Exception as e:
            logger.error("Agent error for %s: %s", session_key, e)
            return f"Error: {e}"

    def _get_or_create_agent(self, session_key: str, source: SessionSource) -> Any:
        """
        从缓存获取 Agent 实例，不存在则创建。
        
        缓存的意义：
        - 保持对话历史的连续性
        - 在支持 prompt caching 的 provider 上节省成本（Anthropic 可节省 ~90%）
        - 避免每条消息都重建系统提示
        """
        if session_key in self._agent_cache:
            return self._agent_cache[session_key]

        # 注入平台上下文到系统提示
        context_prompt = self._build_context_prompt(source)
        agent = self._create_agent(context_prompt)
        self._agent_cache[session_key] = agent
        return agent

    def _build_context_prompt(self, source: SessionSource) -> str:
        """构建平台上下文提示——让 Agent 知道自己在和谁对话"""
        parts = [f"You are connected via {source.platform.value}."]
        parts.append(f"Chat: {source.description}")
        if source.user_name:
            parts.append(f"User: {source.user_name}")
        connected = [p.value for p in self.adapters]
        if len(connected) > 1:
            parts.append(f"Connected platforms: {', '.join(connected)}")
        return " ".join(parts)

    def _create_agent(self, context_prompt: str) -> Any:
        """创建 Agent 实例——这里接入你的 Agent 实现"""
        # 接入前面教程中实现的 Agent
        from agent.agent import Agent
        return Agent(extra_system_prompt=context_prompt)

    # ── 断线重连 ──────────────────────────────────────
    async def _reconnect_watcher(self) -> None:
        """
        后台重连监视器。
        
        指数退避：30s → 60s → 120s → 240s → 300s（上限）
        最多尝试 20 次后放弃。
        """
        MAX_ATTEMPTS = 20
        BACKOFF_CAP = 300

        await asyncio.sleep(10)  # 等启动完成
        while self._running:
            if not self._failed_platforms:
                await asyncio.sleep(30)
                continue

            now = time.monotonic()
            for platform in list(self._failed_platforms):
                info = self._failed_platforms[platform]
                if now < info["next_retry"]:
                    continue
                if info["attempts"] >= MAX_ATTEMPTS:
                    logger.warning("Giving up reconnecting %s", platform.value)
                    del self._failed_platforms[platform]
                    continue

                attempt = info["attempts"] + 1
                logger.info("Reconnecting %s (attempt %d)...", platform.value, attempt)

                try:
                    adapter = self._create_adapter(platform, info["config"])
                    if not adapter:
                        del self._failed_platforms[platform]
                        continue

                    adapter.set_message_handler(self._handle_message)
                    success = await adapter.connect()

                    if success:
                        self.adapters[platform] = adapter
                        del self._failed_platforms[platform]
                        logger.info("✓ %s reconnected", platform.value)
                    else:
                        backoff = min(30 * (2 ** (attempt - 1)), BACKOFF_CAP)
                        info["attempts"] = attempt
                        info["next_retry"] = time.monotonic() + backoff
                except Exception as e:
                    backoff = min(30 * (2 ** (attempt - 1)), BACKOFF_CAP)
                    info["attempts"] = attempt
                    info["next_retry"] = time.monotonic() + backoff
                    logger.warning("Reconnect %s failed: %s", platform.value, e)

            await asyncio.sleep(10)

    async def stop(self) -> None:
        """优雅关闭：停止所有适配器和后台任务"""
        self._running = False
        for platform, adapter in self.adapters.items():
            try:
                adapter.cancel_background_tasks()
                await adapter.disconnect()
            except Exception as e:
                logger.warning("Error disconnecting %s: %s", platform.value, e)
        self.adapters.clear()
        logger.info("Gateway stopped")
```

Runner 的关键设计：

1. **适配器工厂**：`_create_adapter()` 用延迟导入，只在需要时加载平台依赖。Telegram 适配器需要 `python-telegram-bot`，Discord 需要 `discord.py`——不应该强制安装所有依赖。

2. **Agent 缓存**：同一 session key 复用 Agent 实例。这不只是为了保持对话历史——在 Anthropic 等支持 prompt caching 的 provider 上，复用实例可以让系统提示只计费一次，后续请求命中缓存可节省约 90% 的 prompt token 费用。

3. **平台上下文注入**：`_build_context_prompt()` 告诉 Agent 当前对话来自哪个平台、和谁聊天、连接了哪些平台。这让 Agent 能做出平台感知的响应（比如在 Telegram 中不用 @mention 格式）。

4. **指数退避重连**：`30s * 2^(n-1)`，上限 300 秒，最多 20 次。连接错误分为可重试（网络问题）和不可重试（token 无效），后者直接从队列移除。

## 会话重置策略

长期运行的网关需要管理会话的生命周期。不同于 CLI 模式的"一次性对话"，消息平台上的会话可能持续数天甚至数周。会话重置策略定义了何时清空上下文：

```python
# gateway/session_policy.py

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class SessionResetPolicy:
    """
    会话重置策略。
    
    模式：
    - "daily": 每天在指定小时重置
    - "idle": 闲置超时后重置
    - "both": daily 和 idle 哪个先触发就重置
    - "none": 永不自动重置
    """
    mode: str = "both"
    at_hour: int = 4       # daily 重置的小时（0-23，本地时间）
    idle_minutes: int = 1440  # 闲置超时（默认 24 小时）
    notify: bool = True    # 自动重置时是否通知用户


def should_reset_session(
    policy: SessionResetPolicy,
    created_at: datetime,
    updated_at: datetime,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """
    判断会话是否应该重置。
    
    返回 None 表示不重置，否则返回原因字符串（"idle" 或 "daily"）。
    """
    now = now or datetime.now()

    if policy.mode in ("idle", "both"):
        idle_since = now - updated_at
        if idle_since > timedelta(minutes=policy.idle_minutes):
            return "idle"

    if policy.mode in ("daily", "both"):
        # 检查 created_at 和 now 是否跨越了重置时刻
        reset_today = now.replace(
            hour=policy.at_hour, minute=0, second=0, microsecond=0
        )
        if created_at < reset_today <= now:
            return "daily"

    return None
```

配合后台过期检查器使用。网关每隔几分钟扫描一次所有会话，对过期的会话**主动刷新记忆**（把对话摘要存入长期记忆），这样用户下次发消息时不会因为记忆刷新而等待：

```python
async def session_expiry_watcher(self, interval: int = 300):
    """
    后台过期检查器。
    
    每 interval 秒扫描所有会话：
    1. 检查是否满足重置条件
    2. 对过期会话提前刷新记忆
    3. 标记已处理，避免重复刷新
    """
    while self._running:
        for key, entry in list(self._sessions.items()):
            if entry.memory_flushed:
                continue
            reason = should_reset_session(
                policy=self._get_policy(entry),
                created_at=entry.created_at,
                updated_at=entry.updated_at,
            )
            if reason:
                try:
                    await self._flush_memories(entry.session_id)
                    entry.memory_flushed = True
                except Exception as e:
                    logger.warning("Memory flush failed for %s: %s", key, e)

        # 分段 sleep，方便快速响应关闭
        for _ in range(interval):
            if not self._running:
                break
            await asyncio.sleep(1)
```

## 整合到已有 Agent

把前面六篇实现的模块和网关连接起来：

```python
# main_gateway.py

import asyncio
import yaml
from gateway.runner import GatewayRunner


async def main():
    # 加载配置
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    runner = GatewayRunner(config)
    success = await runner.start()
    if not success:
        print("Gateway failed to start")
        return

    # 保持运行直到 Ctrl+C
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        await runner.stop()


if __name__ == "__main__":
    asyncio.run(main())
```

配置文件格式：

```yaml
# config.yaml
platforms:
  telegram:
    enabled: true
    token: "YOUR_BOT_TOKEN"  # 从 @BotFather 获取
    extra:
      media_batch_delay: 0.8

  # discord:
  #   enabled: true
  #   token: "YOUR_DISCORD_TOKEN"
  #   extra: {}

sessions:
  reset_policy:
    mode: both
    at_hour: 4
    idle_minutes: 1440
    notify: true

model:
  default: "claude-sonnet-4-20250514"
  cheap: "claude-haiku-3-20250414"  # 用于智能路由的廉价模型
```

## 扩展新平台

适配器模式的好处是扩展新平台只需三步：

1. **实现适配器类**：继承 `BasePlatformAdapter`，实现 `connect()`、`disconnect()`、`send()`
2. **注册到工厂**：在 `_create_adapter()` 中加一个 `elif` 分支
3. **添加配置**：在 `config.yaml` 的 `platforms` 下加入新平台

不需要改动消息处理逻辑、Agent 调用逻辑或会话管理逻辑——这些都在基类和 Runner 中。

举个例子，如果要加一个简单的 Webhook 入站适配器：

```python
class WebhookAdapter(BasePlatformAdapter):
    """HTTP Webhook 适配器——接收外部系统的消息"""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBHOOK)
        self._server = None

    async def connect(self) -> bool:
        from aiohttp import web

        app = web.Application()
        app.router.add_post("/webhook", self._handle_webhook)

        port = self.config.extra.get("port", 8080)
        runner = web.AppRunner(app)
        await runner.setup()
        self._server = web.TCPSite(runner, "0.0.0.0", port)
        await self._server.start()

        self._running = True
        logger.info("[Webhook] Listening on port %d", port)
        return True

    async def _handle_webhook(self, request) -> web.Response:
        data = await request.json()
        event = MessageEvent(
            text=data.get("text", ""),
            source=self.build_source(
                chat_id=data.get("chat_id", "webhook"),
                user_id=data.get("user_id"),
            ),
        )
        await self.handle_message(event)
        return web.json_response({"ok": True})

    async def send(self, chat_id, content, **kwargs) -> SendResult:
        # Webhook 通常是单向的，响应在 handle 中返回
        return SendResult(success=True)

    async def disconnect(self) -> None:
        if self._server:
            await self._server.stop()
        self._running = False
```

## 小结

本篇实现了多平台消息网关的核心架构：

- **消息归一化**：`MessageEvent` / `SendResult` 统一所有平台的消息格式
- **平台适配器模式**：`BasePlatformAdapter` 基类封装通用逻辑（中断、重试、typing），子类只关注平台对接
- **确定性会话路由**：`build_session_key()` 保证同一用户/群组/线程的消息始终路由到同一会话
- **指数退避重连**：平台断线不影响其他平台，后台自动恢复
- **Agent 缓存**：复用实例保持上下文连续性并节省 prompt caching 成本
- **会话生命周期**：自动过期、主动记忆刷新，长期运行不积压

从 CLI 到多平台网关，Agent 的交互入口从"开发者工具"变成了"随时可用的助手"。
