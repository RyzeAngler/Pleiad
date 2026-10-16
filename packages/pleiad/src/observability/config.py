from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ObservabilityConfig(BaseModel):
    """Configure provider-neutral OpenTelemetry collection behavior."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    service_name: str = Field(default="pleiad", min_length=1, max_length=128)
    environment: str | None = Field(default=None, min_length=1, max_length=64)
    release: str | None = Field(default=None, min_length=1, max_length=128)
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    capture_inputs: bool = False
    capture_outputs: bool = False
    max_content_bytes: int = Field(default=4096, ge=1, le=1_000_000)
    max_attribute_bytes: int = Field(default=512, ge=1, le=64_000)
    max_attributes: int = Field(default=32, ge=1, le=256)
    max_array_items: int = Field(default=16, ge=1, le=256)
    max_total_attribute_bytes: int = Field(default=8192, ge=1, le=1_000_000)
    shutdown_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)


class LangfuseConfig(ObservabilityConfig):
    """Configure an explicitly owned Langfuse OpenTelemetry provider."""

    public_key: SecretStr
    secret_key: SecretStr
    base_url: str = Field(min_length=1, max_length=2048)
    request_timeout_seconds: int = Field(default=5, ge=1, le=60)
    flush_at: int = Field(default=64, ge=1, le=10_000)
    flush_interval_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
