"""Global model-role registry used by dynamically assembled Agents."""

from dataclasses import dataclass

from pydantic import Field

from pleiad.config import ModelRole, Settings
from pleiad.contracts import ContractModel
from pleiad.llm.client import LLMClient
from pleiad.llm.config import LLMConfig
from pleiad.llm.factory import create_client


class ModelProfile(ContractModel):
    """Deployment metadata kept at process scope, never copied into a Node."""

    code: str = Field(min_length=1, max_length=128)
    role: ModelRole
    protocol: str
    provider: str
    model: str
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """A public profile paired with its provider-neutral client."""

    profile: ModelProfile
    client: LLMClient


class ModelRegistry:
    """Resolve semantic roles to globally configured model clients."""

    def __init__(self) -> None:
        self._bindings: dict[ModelRole, ModelBinding] = {}

    def register(self, binding: ModelBinding) -> None:
        """Register one model role exactly once."""
        if binding.profile.role in self._bindings:
            raise ValueError(f"model role is already registered: {binding.profile.role.value}")
        self._bindings[binding.profile.role] = binding

    def resolve(self, role: ModelRole) -> ModelBinding:
        """Resolve a required semantic role."""
        try:
            return self._bindings[role]
        except KeyError as exc:
            raise LookupError(f"no global model configured for role: {role.value}") from exc

    def roles(self) -> tuple[ModelRole, ...]:
        """Return configured roles in stable order."""
        return tuple(sorted(self._bindings, key=lambda role: role.value))

    async def aclose(self) -> None:
        """Close all unique provider clients."""
        clients = {id(binding.client): binding.client for binding in self._bindings.values()}
        for client in clients.values():
            await client.aclose()


def create_model_registry(settings: Settings) -> ModelRegistry:
    """Build the global registry from process settings and optional role models."""
    registry = ModelRegistry()
    _register_role(
        registry,
        settings,
        ModelRole.TEXT,
        settings.llm_model,
        settings.llm_base_url,
        "pleiad.model.text",
    )
    if settings.llm_vision_model:
        _register_role(
            registry,
            settings,
            ModelRole.VISION,
            settings.llm_vision_model,
            settings.llm_vision_base_url or settings.llm_base_url,
            "pleiad.model.vision",
        )
    if settings.llm_speech_model:
        _register_role(
            registry,
            settings,
            ModelRole.SPEECH,
            settings.llm_speech_model,
            settings.llm_speech_base_url or settings.llm_base_url,
            "pleiad.model.speech",
        )
    return registry


def _register_role(
    registry: ModelRegistry,
    settings: Settings,
    role: ModelRole,
    model: str,
    base_url: str | None,
    code: str,
) -> None:
    config = LLMConfig(
        protocol=settings.llm_protocol,
        provider=settings.llm_provider,
        model=model,
        api_key=settings.llm_api_key,
        base_url=base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    registry.register(
        ModelBinding(
            profile=ModelProfile(
                code=code,
                role=role,
                protocol=config.protocol.value,
                provider=config.provider.value,
                model=config.model,
                capabilities=("text",) if role is ModelRole.TEXT else (role.value,),
            ),
            client=create_client(config),
        )
    )
