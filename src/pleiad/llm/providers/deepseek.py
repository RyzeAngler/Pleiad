"""DeepSeek provider profile using the OpenAI SDK chat compatibility mode."""

from pleiad.llm.capabilities import LLMCapabilities
from pleiad.llm.config import LLMProtocol, LLMProvider
from pleiad.llm.contracts import ResponseFormatType
from pleiad.llm.registry import ProviderProfile


def profile() -> ProviderProfile:
    """Return a conservative DeepSeek profile requiring an explicit endpoint."""
    return ProviderProfile(
        protocol=LLMProtocol.OPENAI,
        provider=LLMProvider.DEEPSEEK,
        wire_mode="chat_completions",
        default_base_url=None,
        capabilities=LLMCapabilities(
            thinking=True,
            response_formats=frozenset(
                {ResponseFormatType.TEXT, ResponseFormatType.JSON_OBJECT},
            ),
            tool_calling=True,
            streaming=True,
        ),
        request_options={
            "max_tokens_field": "max_tokens",
            "thinking_mode": "deepseek",
            "thinking_response_fields": ("reasoning_content",),
        },
        allowed_provider_options=frozenset({"clear_thinking"}),
    )
