from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MemorySettings(BaseSettings):
    """Bind local memory management without requiring model or Sandbox settings."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="PLEIAD_", extra="ignore", frozen=True)

    workspace: Path = Path("workspace")
    memory_user_id: str = Field(default="default-user", min_length=1)
    memory_cross_space: bool = False

    @field_validator("workspace")
    @classmethod
    def resolve_workspace(cls, value: Path) -> Path:
        """Resolve the host workspace independently of model-supplied paths."""
        return value.expanduser().resolve()

    @property
    def runtime_root(self) -> Path:
        """Return the new-format persistence root without opening legacy data."""
        return self.workspace / "runtime-v2"


class Settings(MemorySettings):
    """Own deployment settings independently from the reusable SDK."""

    capability_file: Path | None = None
    observe_directory: Path | None = None
    observe_max_files: int = Field(default=1000, ge=1)
    observe_max_file_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    observe_max_total_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    llm_protocol: Literal["openai", "anthropic"] = "openai"
    llm_provider: Literal["openai", "anthropic", "deepseek", "glm", "qwen", "kimi", "minimax"] = "deepseek"
    llm_model: str = Field(default="deepseek-v4-flash", min_length=1)
    llm_vision_model: str | None = None
    llm_vision_base_url: str | None = None
    llm_speech_model: str | None = None
    llm_speech_base_url: str | None = None
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str | None = None
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    memory_recall_max_results: int = Field(default=5, ge=1, le=20)
    approval_tool_codes: frozenset[str] = frozenset()
    log_level: str = "INFO"
    log_console: bool = True
    log_directory: Path | None = None
    log_backup_count: int = Field(default=7, ge=1)
    log_max_file_bytes: int = Field(default=30 * 1024 * 1024, ge=1024 * 1024)
    sandbox_runtime: Literal["docker", "podman"] = "docker"
    sandbox_image: str = Field(min_length=1)

    @field_validator("observe_directory", mode="before")
    @classmethod
    def resolve_observe_directory(cls, value: object) -> Path | None:
        """Keep an unset or blank source disabled and resolve only explicit host paths."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str | Path):
            raise ValueError("The read-only directory source requires a host path")
        return Path(value).expanduser().absolute()

    max_workers: int = Field(default=3, ge=1, le=32)
    queue_capacity: int = Field(default=1_000, ge=1, le=100_000)

    @field_validator("approval_tool_codes")
    @classmethod
    def validate_approval_codes(cls, value: frozenset[str]) -> frozenset[str]:
        """Reject blank host-authored Tool identities."""
        if any(not code.strip() for code in value):
            raise ValueError("Approval Tool codes must not be blank")
        return value

    @property
    def resolved_capability_file(self) -> Path:
        """Return the public capability manifest for this deployment."""
        return (
            self.workspace / "capabilities.yaml" if self.capability_file is None else self.capability_file
        ).resolve()
