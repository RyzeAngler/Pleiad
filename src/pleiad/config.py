"""Process configuration and global model-role settings."""

from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pleiad.llm.config import LLMProtocol, LLMProvider


class ModelRole(StrEnum):
    """Semantic model roles understood by dynamically generated Agents."""

    TEXT = "text"
    VISION = "vision"
    SPEECH = "speech"


class Settings(BaseSettings):
    """Application settings; capability YAML never contains these values."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PLEIAD_",
        extra="ignore",
        frozen=True,
    )

    workspace: Path = Field(default=Path("workspace"))
    capability_file: Path | None = None
    llm_protocol: LLMProtocol = LLMProtocol.OPENAI
    llm_provider: LLMProvider = LLMProvider.DEEPSEEK
    llm_model: str = Field(default="deepseek-v4-flash", min_length=1)
    llm_vision_model: str | None = None
    llm_vision_base_url: str | None = None
    llm_speech_model: str | None = None
    llm_speech_base_url: str | None = None
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str | None = None
    llm_timeout_seconds: float = Field(default=120.0, gt=0.0, le=3_600.0)
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    memory_user_id: str = Field(default="default-user", min_length=1)
    memory_cross_conversation: bool = False
    memory_search_backend: str = Field(default="lexical", min_length=1)
    memory_profile_max_items: int = Field(default=20, ge=1, le=100)
    memory_profile_max_tokens: int = Field(default=2_000, ge=128, le=32_000)
    memory_recall_max_calls: int = Field(default=3, ge=1, le=20)
    memory_recall_max_results: int = Field(default=5, ge=1, le=20)
    memory_recall_max_output_tokens: int = Field(default=2_000, ge=128, le=32_000)
    log_level: str = "INFO"
    log_console: bool = True

    @field_validator("workspace")
    @classmethod
    def workspace_is_absolute_or_resolved(cls, value: Path) -> Path:
        """Resolve a relative workspace against the current process directory."""
        return value.expanduser().resolve()

    @property
    def resolved_capability_file(self) -> Path:
        """Return the capability index path for this workspace."""
        return (self.capability_file or self.workspace / "capabilities.yaml").resolve()

    @property
    def runtime_root(self) -> Path:
        """Return the directory for execution state and events."""
        return self.workspace / "runtime"
