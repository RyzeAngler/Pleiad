"""Anthropic provider profile."""

from pleiad.llm.capabilities import LLMCapabilities
from pleiad.llm.config import LLMProtocol, LLMProvider
from pleiad.llm.contracts import ResponseFormatType
from pleiad.llm.registry import ProviderProfile


def profile() -> ProviderProfile:
    """Return the Anthropic Messages SDK profile."""
    return ProviderProfile(
        protocol=LLMProtocol.ANTHROPIC,
        provider=LLMProvider.ANTHROPIC,
        wire_mode="messages",
        default_base_url="https://api.anthropic.com/v1",
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
