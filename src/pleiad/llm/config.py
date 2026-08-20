"""Configuration for selecting an LLM protocol and provider."""

from enum import StrEnum

from pydantic import Field, SecretStr

from pleiad.llm.contracts import JsonObject, LLMModel


class LLMProtocol(StrEnum):
    """Public wire protocols supported by Pleiad's LLM layer."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class LLMProvider(StrEnum):
    """Built-in model providers supported by Pleiad's LLM layer."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    KIMI = "kimi"
    DEEPSEEK = "deepseek"
    GLM = "glm"
    QWEN = "qwen"
    MINIMAX = "minimax"


class LLMConfig(LLMModel):
    """Immutable configuration used by the LLM Factory."""

    protocol: LLMProtocol
    provider: LLMProvider
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretStr
    base_url: str | None = Field(default=None, min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0.0, le=3_600.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    default_headers: dict[str, str] = Field(default_factory=dict)
    provider_options: JsonObject = Field(default_factory=dict)
