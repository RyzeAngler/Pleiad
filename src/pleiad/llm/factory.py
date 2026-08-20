"""Factory for official-SDK-backed provider clients."""

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from pleiad.llm.client import LLMClient
from pleiad.llm.config import LLMConfig, LLMProtocol
from pleiad.llm.errors import LLMConfigurationError
from pleiad.llm.protocols.anthropic import AnthropicClient
from pleiad.llm.protocols.openai import OpenAIClient
from pleiad.llm.providers.catalog import builtin_profiles
from pleiad.llm.registry import ProviderRegistry


def create_client(
    config: LLMConfig,
    *,
    sdk_client: AsyncOpenAI | AsyncAnthropic | None = None,
    registry: ProviderRegistry | None = None,
) -> LLMClient:
    """Create an official SDK client from a public protocol and provider name.

    The SDK is selected by the public protocol. The selected provider profile
    controls base URL, headers, capability gates, parameter mapping, and
    response normalization.
    """
    selected_registry = registry or ProviderRegistry(builtin_profiles())
    profile = selected_registry.get(config.protocol, config.provider)
    if config.base_url is None and profile.default_base_url is None:
        raise LLMConfigurationError(f"base_url is required for provider: {config.provider}")
    if config.protocol is LLMProtocol.OPENAI:
        if sdk_client is not None and not isinstance(sdk_client, AsyncOpenAI):
            raise LLMConfigurationError("openai protocol requires an AsyncOpenAI client")
        return OpenAIClient(config, profile, sdk_client=sdk_client)
    if config.protocol is LLMProtocol.ANTHROPIC:
        if sdk_client is not None and not isinstance(sdk_client, AsyncAnthropic):
            raise LLMConfigurationError("anthropic protocol requires an AsyncAnthropic client")
        return AnthropicClient(config, profile, sdk_client=sdk_client)
    raise LLMConfigurationError(f"unsupported protocol: {config.protocol.value}")
