"""Qwen provider profile using DashScope's OpenAI compatibility mode."""

from pleiad.llm.capabilities import LLMCapabilities
from pleiad.llm.config import LLMProtocol, LLMProvider
from pleiad.llm.contracts import ResponseFormatType
from pleiad.llm.registry import ProviderProfile


def profile() -> ProviderProfile:
    """Return a conservative Qwen profile requiring an explicit endpoint."""
    return ProviderProfile(
        protocol=LLMProtocol.OPENAI,
        provider=LLMProvider.QWEN,
        wire_mode="chat_completions",
        default_base_url=None,
        capabilities=LLMCapabilities(
            thinking=True,
            thinking_budget=True,
            response_formats=frozenset(
                {ResponseFormatType.TEXT, ResponseFormatType.JSON_OBJECT},
            ),
            tool_calling=True,
            streaming=True,
        ),
        request_options={
            "max_tokens_field": "max_completion_tokens",
            "thinking_mode": "qwen",
            "thinking_response_fields": ("reasoning_content",),
        },
        allowed_provider_options=frozenset({"enable_thinking", "thinking_budget"}),
    )
