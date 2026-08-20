"""MiniMax profiles for OpenAI and Anthropic compatibility modes."""

from pleiad.llm.capabilities import LLMCapabilities
from pleiad.llm.config import LLMProtocol, LLMProvider
from pleiad.llm.contracts import ResponseFormatType
from pleiad.llm.registry import ProviderProfile


def profile() -> ProviderProfile:
    """Return the MiniMax OpenAI SDK compatibility profile."""
    return ProviderProfile(
        protocol=LLMProtocol.OPENAI,
        provider=LLMProvider.MINIMAX,
        wire_mode="chat_completions",
        default_base_url="https://api.minimaxi.com/v1",
        capabilities=LLMCapabilities(
            thinking=True,
            response_formats=frozenset(
                {ResponseFormatType.TEXT, ResponseFormatType.JSON_OBJECT},
            ),
            tool_calling=True,
            streaming=True,
        ),
        request_options={
            "max_tokens_field": "max_completion_tokens",
            "thinking_mode": "minimax",
            "thinking_field": "reasoning_content",
            "thinking_response_fields": (
                "reasoning_details",
                "reasoning_content",
                "reasoning",
            ),
        },
        allowed_provider_options=frozenset({"reasoning_split"}),
    )


def anthropic_profile() -> ProviderProfile:
    """Return the MiniMax Anthropic SDK compatibility profile."""
    return ProviderProfile(
        protocol=LLMProtocol.ANTHROPIC,
        provider=LLMProvider.MINIMAX,
        wire_mode="messages",
        default_base_url="https://api.minimaxi.com/anthropic/v1",
        capabilities=LLMCapabilities(
            thinking=True,
            thinking_budget=True,
            response_formats=frozenset(
                {
                    ResponseFormatType.TEXT,
                    ResponseFormatType.JSON_OBJECT,
                    ResponseFormatType.JSON_SCHEMA,
                },
            ),
            strict_json_schema=True,
            tool_calling=True,
            parallel_tool_calling=True,
            streaming=True,
        ),
        default_headers=(("anthropic-version", "2023-06-01"),),
        request_options={"default_max_tokens": 4_096},
    )
