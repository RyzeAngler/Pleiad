import re

from pydantic import ConfigDict, Field, field_validator

from pleiad.contracts import ContractModel

PINNED_IMAGE_PATTERN = re.compile(r"^[^\s]+@sha256:[0-9a-fA-F]{64}$")
NUMERIC_USER_PATTERN = re.compile(r"^(?P<uid>[1-9][0-9]*):(?P<gid>[1-9][0-9]*)$")


class SandboxPolicy(ContractModel):
    """Host-authored resource and security policy for one Sandbox request."""

    model_config = ConfigDict(strict=True)

    image: str = Field(min_length=1)
    timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    cleanup_timeout_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    memory_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    cpus: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    pids_limit: int = Field(default=128, gt=0)
    max_output_bytes: int = Field(default=1_048_576, gt=0)
    user: str = Field(default="65532:65532", min_length=3)
    allow_shell_command: bool = False
    allowed_environment: frozenset[str] = frozenset()

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        """Require a fixed, non-latest image digest."""
        if not value.strip() or PINNED_IMAGE_PATTERN.fullmatch(value) is None:
            raise ValueError("sandbox image must be a non-latest sha256 digest reference")
        if ":latest@" in value:
            raise ValueError("sandbox image must not use the latest tag")
        return value

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        """Require a numeric, non-root UID:GID pair."""
        match = NUMERIC_USER_PATTERN.fullmatch(value)
        if match is None or int(match.group("uid")) == 0 or int(match.group("gid")) == 0:
            raise ValueError("sandbox user must be a non-root numeric UID:GID pair")
        return value

    @field_validator("allowed_environment")
    @classmethod
    def validate_environment_names(cls, value: frozenset[str]) -> frozenset[str]:
        """Reject malformed environment allowlist names."""
        if any(not name.strip() or "=" in name or "\x00" in name or "\n" in name for name in value):
            raise ValueError("sandbox environment names must be non-empty safe names")
        return value
