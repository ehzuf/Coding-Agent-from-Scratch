"""
LLM 工厂函数

根据配置自动选择并实例化对应的后端，调用方不需要关心具体后端类。

支持的 provider：
  - "anthropic" : Claude 系列模型
  - "openai"    : GPT 系列及所有 OpenAI 兼容服务（DeepSeek、Ollama 等）
"""

from .base import BaseLLM, LLMResponse
from .anthropic_llm import AnthropicLLM
from .openai_llm import OpenAILLM


def create_llm(
    provider: str,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    extra_body: dict | None = None,
    enable_cache: bool = True,
) -> BaseLLM:
    """
    创建 LLM 实例。

    Args:
        provider:     "anthropic" 或 "openai"
        model:        模型名称，如 "claude-opus-4-5" 或 "gpt-4o"
        api_key:      API 密钥，不传则从环境变量读取
        base_url:     自定义 API 地址（仅 openai 后端有效，用于兼容服务）
        extra_body:   透传给每次请求的额外参数（仅 openai 后端有效）
                      例如禁用某些兼容服务的思考模式：{"enable_thinking": False}
        enable_cache: 是否启用 prompt caching
                      - Anthropic：手动在 system/messages 中添加 cache_control 标记
                      - OpenAI：自动 caching，此参数只影响 cache 用量的读取显示

    Returns:
        BaseLLM 实例

    Raises:
        ValueError: 不支持的 provider
    """
    if provider == "anthropic":
        return AnthropicLLM(model=model, api_key=api_key, enable_cache=enable_cache)
    elif provider == "openai":
        return OpenAILLM(
            model=model,
            api_key=api_key,
            base_url=base_url,
            extra_body=extra_body,
        )
    else:
        raise ValueError(
            f"不支持的 provider: {provider!r}，可选值: 'anthropic', 'openai'"
        )


__all__ = ["BaseLLM", "LLMResponse", "AnthropicLLM", "OpenAILLM", "create_llm"]
