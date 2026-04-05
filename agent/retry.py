"""
重试机制 —— 指数退避重试

对应 reference 中的 utils/withRetry.ts

核心功能：
  - 网络错误指数退避重试
  - 工具失败优雅处理
  - 可配置的重试次数和延迟

重试策略：
  - 初始延迟：1 秒
  - 退避因子：2（每次延迟翻倍）
  - 最大延迟：60 秒
  - 默认重试次数：3
"""

import time
import random
from typing import Callable, TypeVar, Optional
from functools import wraps


T = TypeVar("T")


class RetryError(Exception):
    """重试耗尽后的错误。"""

    def __init__(self, message: str, last_exception: Optional[Exception] = None):
        super().__init__(message)
        self.last_exception = last_exception


def with_retry(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[Exception, int, float], None]] = None,
) -> Callable:
    """
    装饰器：为函数添加重试机制。

    Args:
        max_retries: 最大重试次数
        initial_delay: 初始延迟（秒）
        max_delay: 最大延迟（秒）
        backoff_factor: 退避因子
        retryable_exceptions: 可重试的异常类型
        on_retry: 重试时的回调函数 (exception, attempt, delay)

    Returns:
        装饰后的函数
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            delay = initial_delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e

                    if attempt >= max_retries:
                        # 重试次数耗尽
                        raise RetryError(
                            f"函数 {func.__name__} 在 {max_retries} 次重试后仍然失败",
                            last_exception=e,
                        ) from e

                    # 计算下次延迟（带抖动）
                    jitter = random.uniform(0, 0.1 * delay)
                    sleep_time = min(delay + jitter, max_delay)

                    if on_retry:
                        on_retry(e, attempt + 1, sleep_time)

                    time.sleep(sleep_time)
                    delay *= backoff_factor

            # 不应该到达这里
            raise RetryError("未知错误") from last_exception

        return wrapper
    return decorator


def retry_llm_call(
    func: Callable[..., T],
    max_retries: int = 3,
    **kwargs
) -> T:
    """
    专门用于 LLM 调用的重试包装。

    自动处理常见的 LLM API 错误：
    - 网络超时
    - 速率限制
    - 服务暂时不可用

    Args:
        func: 要执行的函数
        max_retries: 最大重试次数
        **kwargs: 传递给 with_retry 的其他参数

    Returns:
        函数返回值
    """
    # 常见的可重试异常
    retryable = (
        ConnectionError,
        TimeoutError,
        Exception,  # 兜底，实际应该更具体
    )

    @with_retry(
        max_retries=max_retries,
        retryable_exceptions=retryable,
        **kwargs
    )
    def wrapped():
        return func()

    return wrapped()


class ToolError(Exception):
    """工具执行错误。"""
    pass


def safe_tool_call(
    tool_call: Callable[..., str],
    *args,
    default_error_message: str = "工具执行失败",
    **kwargs
) -> str:
    """
    安全地调用工具，捕获所有异常。

    Args:
        tool_call: 工具函数
        *args: 位置参数
        default_error_message: 默认错误消息
        **kwargs: 关键字参数

    Returns:
        工具返回结果，或错误消息
    """
    try:
        return tool_call(*args, **kwargs)
    except ToolError as e:
        return f"错误：{e}"
    except Exception as e:
        return f"{default_error_message}: {type(e).__name__}: {e}"
