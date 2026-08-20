"""OpenAI provider profile."""

from pleiad.llm.capabilities import LLMCapabilities
from pleiad.llm.config import LLMProtocol, LLMProvider
from pleiad.llm.contracts import ResponseFormatType
from pleiad.llm.registry import ProviderProfile


def profile() -> ProviderProfile:
    """Return the OpenAI Responses SDK profile."""
    return ProviderProfile(
        protocol=LLMProtocol.OPENAI,
        provider=LLMProvider.OPENAI,
        wire_mode="responses",
        default_base_url="https://api.openai.com/v1",
        capabilities=LLMCapabilities(
            thinking=True,
            response_formats=frozenset(
                {ResponseFormatType.TEXT, ResponseFormatType.JSON_SCHEMA},
            ),
            strict_json_schema=True,
            tool_calling=True,
            strict_tool_calling=True,
            parallel_tool_calling=True,
            streaming=True,
        ),
        request_options={
            "thinking_mode": "reasoning_effort",
            "thinking_response_fields": ("reasoning_content", "reasoning"),
        },
    )
