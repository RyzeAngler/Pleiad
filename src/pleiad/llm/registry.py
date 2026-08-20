"""Registry of protocol/provider profiles and explicit parameter policies."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pleiad.llm.capabilities import LLMCapabilities
from pleiad.llm.config import LLMProtocol, LLMProvider
from pleiad.llm.errors import LLMConfigurationError


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """Static SDK, wire, capability, and conversion policy for one provider."""

    protocol: LLMProtocol
    provider: LLMProvider
    wire_mode: str
    default_base_url: str | None
    capabilities: LLMCapabilities
    default_headers: tuple[tuple[str, str], ...] = ()
    request_options: Mapping[str, Any] = field(default_factory=dict)
    allowed_provider_options: frozenset[str] = frozenset()


class ProviderRegistry:
    """Resolve immutable protocol/provider profiles."""

    def __init__(self, profiles: Iterable[ProviderProfile] = ()) -> None:
        """Create a registry from a finite set of provider profiles."""
        self._profiles: dict[tuple[LLMProtocol, LLMProvider], ProviderProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: ProviderProfile) -> None:
        """Register one profile and reject duplicate protocol/provider keys."""
        key = (profile.protocol, profile.provider)
        if key in self._profiles:
            raise LLMConfigurationError(
                f"provider profile already registered: {profile.protocol.value}/{profile.provider}",
            )
        self._profiles[key] = profile

    def get(self, protocol: LLMProtocol, provider: LLMProvider) -> ProviderProfile:
        """Return one profile or fail with a stable configuration error."""
        try:
            return self._profiles[(protocol, provider)]
        except KeyError as error:
            raise LLMConfigurationError(
                f"unsupported protocol/provider: {protocol.value}/{provider}",
            ) from error

    def list(self) -> tuple[ProviderProfile, ...]:
        """Return registered profiles in canonical order."""
        return tuple(self._profiles[key] for key in sorted(self._profiles))
