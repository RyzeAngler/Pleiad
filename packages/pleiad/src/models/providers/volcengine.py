from collections.abc import Mapping

from pydantic import SecretStr

from pleiad.contracts import JsonValue
from pleiad.models.providers.openai import OpenAIProvider as OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    """Bind Ark's documented OpenAI-compatible Chat API to the shared SDK transport."""

    def __init__(
        self,
        *,
        model: str,
        api_key: SecretStr,
        base_url: str | None,
        timeout_seconds: float,
        max_retries: int,
        provider_options: Mapping[str, JsonValue],
        headers: Mapping[str, str],
    ) -> None:
        """Select the native Ark endpoint while retaining Chat protocol semantics."""
        super().__init__(
            model=model,
            api_key=api_key,
            base_url="https://ark.cn-beijing.volces.com/api/v3" if base_url is None else base_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            provider_options=provider_options,
            headers=headers,
        )
