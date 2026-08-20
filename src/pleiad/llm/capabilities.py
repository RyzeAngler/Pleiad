"""Capability declarations and request checks for LLM providers."""

from dataclasses import dataclass

from pleiad.llm.config import LLMProtocol, LLMProvider
from pleiad.llm.contracts import LLMRequest, ResponseFormatType
from pleiad.llm.errors import UnsupportedCapabilityError


@dataclass(frozen=True, slots=True)
class LLMCapabilities:
    """Provider capabilities relevant to Pleiad's normalized request contract."""

    thinking: bool = False
    thinking_budget: bool = False
    response_formats: frozenset[ResponseFormatType] = frozenset({ResponseFormatType.TEXT})
    strict_json_schema: bool = False
    tool_calling: bool = False
    strict_tool_calling: bool = False
    parallel_tool_calling: bool = False
    streaming: bool = False


def require_capabilities(
    request: LLMRequest,
    capabilities: LLMCapabilities,
    *,
    protocol: LLMProtocol,
    provider: LLMProvider,
) -> None:
    """Reject a request that the selected provider cannot satisfy.

    Args:
        request: Provider-neutral request to validate.
        capabilities: Effective provider capabilities.
        protocol: Selected public protocol name.
        provider: Selected provider name.

    Raises:
        UnsupportedCapabilityError: A requested feature is unavailable.
    """
    if request.thinking.enabled and not capabilities.thinking:
        _unsupported("thinking", protocol, provider)
    if request.thinking.budget_tokens is not None and not capabilities.thinking_budget:
        _unsupported("thinking token budget", protocol, provider)
    response_format = request.response_format.type
    if response_format not in capabilities.response_formats:
        _unsupported(f"{response_format.value} output", protocol, provider)
    if (
        response_format is ResponseFormatType.JSON_SCHEMA
        and request.response_format.strict
        and not capabilities.strict_json_schema
    ):
        _unsupported("strict JSON Schema output", protocol, provider)
    if request.tools and not capabilities.tool_calling:
        _unsupported("Tool Calling", protocol, provider)
    if request.stream and not capabilities.streaming:
        _unsupported("streaming", protocol, provider)


def _unsupported(feature: str, protocol: LLMProtocol, provider: LLMProvider) -> None:
    """Raise one normalized unsupported-capability error."""
    raise UnsupportedCapabilityError(
        f"provider does not support {feature}",
        protocol=protocol,
        provider=provider,
    )
