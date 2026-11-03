from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.openai.client import OpenAIModelClient

from pleiad_weave.infrastructure.config import Settings


def create_model(settings: Settings, role: str = "text") -> OpenAIModelClient | AnthropicModelClient:
    """Bind one product model role directly to a public SDK protocol client.

    Args:
        settings: Host-owned provider credentials and endpoint settings.
        role: Explicit text, vision, or speech role.

    Returns:
        The configured protocol client.

    Raises:
        ValueError: If the requested role has no configured model.
    """
    model: str | None
    if role == "text":
        model, base_url = settings.llm_model, settings.llm_base_url
    elif role == "vision":
        model, base_url = settings.llm_vision_model, settings.llm_vision_base_url
    elif role == "speech":
        model, base_url = settings.llm_speech_model, settings.llm_speech_base_url
    else:
        raise ValueError(f"Unknown model role: {role}")
    if model is None:
        raise ValueError(f"No model configured for role: {role}")
    client = OpenAIModelClient if settings.llm_protocol == "openai" else AnthropicModelClient
    return client(
        provider=settings.llm_provider,
        model=model,
        api_key=settings.llm_api_key,
        base_url=base_url if base_url is not None else settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
