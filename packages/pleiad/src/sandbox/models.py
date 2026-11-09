import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

from pydantic import Field

from pleiad.contracts import ContractModel, JsonObject
from pleiad.sandbox.policy import SandboxPolicy

MAX_STDIN_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class SandboxMount:
    """One explicitly permitted host-to-container bind mount."""

    source: Path
    target: PurePosixPath
    read_only: bool = True
    mask: bool = False

    def __post_init__(self) -> None:
        """Validate and normalize a host source and absolute POSIX target."""
        if not self.source.is_absolute():
            raise ValueError("sandbox source mount path must be absolute")
        if not self.source.exists():
            raise ValueError("sandbox source mount path must exist")
        normalized_source = self.source.resolve(strict=True)
        if normalized_source == Path("/"):
            raise ValueError("sandbox source mount path must not be the host root")
        if normalized_source.is_socket() or normalized_source.is_block_device() or normalized_source.is_char_device():
            raise ValueError("sandbox source mount path must not be a socket or device")
        if self.mask and (
            not self.read_only
            or self.source != normalized_source
            or not normalized_source.is_dir()
            or any(normalized_source.iterdir())
        ):
            raise ValueError("sandbox masks require an empty real directory mounted read-only")
        if not self.target.is_absolute():
            raise ValueError("sandbox target mount path must be absolute")
        if self.target == PurePosixPath("/"):
            raise ValueError("sandbox target mount path must not be root")
        if any(part in {".", ".."} for part in self.target.parts):
            raise ValueError("sandbox target mount path must not contain dot segments")
        if any(character in str(normalized_source) for character in (",", "\x00", "\n")):
            raise ValueError("sandbox source mount path contains unsupported characters")
        if any(character in str(self.target) for character in (",", "\x00", "\n")):
            raise ValueError("sandbox target mount path contains unsupported characters")
        object.__setattr__(self, "source", normalized_source)


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    """A fully resolved argv or explicitly authorized container shell command."""

    cwd: PurePosixPath
    policy: SandboxPolicy
    argv: tuple[str, ...] | None = None
    shell_command: str | None = None
    mounts: tuple[SandboxMount, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    stdin: bytes | None = None
    operation_id: str | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous commands, unsafe paths, and mutable request state."""
        if self.operation_id is not None and (
            not self.operation_id.strip()
            or len(self.operation_id) > 256
            or any(character in self.operation_id for character in ("\x00", "\n", "\r"))
        ):
            raise ValueError("Sandbox operation identities must be nonblank bounded text")
        if (self.argv is None) == (self.shell_command is None):
            raise ValueError("sandbox request must contain exactly one of argv or shell_command")
        if self.argv is not None:
            if not self.argv or not self.argv[0]:
                raise ValueError("sandbox argv must contain a non-empty entrypoint")
            if any("\x00" in item for item in self.argv):
                raise ValueError("sandbox argv entries must not contain NUL")
            object.__setattr__(self, "argv", tuple(self.argv))
        if self.shell_command is not None:
            if not self.shell_command.strip() or "\x00" in self.shell_command:
                raise ValueError("sandbox shell command must be non-empty and contain no NUL")
            if not self.policy.allow_shell_command:
                raise ValueError("sandbox shell commands require an explicit policy allowance")
        if not self.cwd.is_absolute():
            raise ValueError("sandbox cwd must be an absolute POSIX path")
        if any(part in {".", ".."} for part in self.cwd.parts):
            raise ValueError("sandbox cwd must not contain dot segments")
        if any(character in str(self.cwd) for character in ("\x00", "\n", "\\")):
            raise ValueError("sandbox cwd contains unsupported characters")
        if any(
            not key.strip() or "=" in key or "\x00" in key or "\n" in key or "\x00" in value or "\n" in value
            for key, value in self.environment.items()
        ):
            raise ValueError("sandbox environment contains an unsupported name or value")
        invalid_environment = set(self.environment) - self.policy.allowed_environment
        if invalid_environment:
            raise ValueError("sandbox environment contains a name outside its policy")
        if self.stdin is not None:
            if not isinstance(self.stdin, bytes):
                raise ValueError("sandbox stdin must be bytes")
            if len(self.stdin) > MAX_STDIN_BYTES:
                raise ValueError(f"sandbox stdin must not exceed {MAX_STDIN_BYTES} bytes")
        validate_mounts(self.mounts)
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    def digest(self) -> str:
        """Hash the resolved backend request without exposing its command or secrets."""
        policy = self.policy.model_dump(mode="json")
        policy["allowed_environment"] = sorted(self.policy.allowed_environment)
        payload = {
            "cwd": str(self.cwd),
            "argv": self.argv,
            "shell_command": self.shell_command,
            "mounts": [(str(value.source), str(value.target), value.read_only, value.mask) for value in self.mounts],
            "environment": dict(self.environment),
            "policy": policy,
            "stdin_digest": None if self.stdin is None else hashlib.sha256(self.stdin).hexdigest(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_mounts(mounts: tuple[SandboxMount, ...]) -> None:
    """Allow empty read-only masks beneath one explicit host mount.

    Args:
        mounts: Host-selected mounts in installation order.

    Raises:
        ValueError: If targets overlap without a valid restrictive mask.
    """
    for index, mount in enumerate(mounts):
        if mount.mask:
            parents = tuple(
                (parent_index, parent)
                for parent_index, parent in enumerate(mounts)
                if parent.target != mount.target and mount.target.is_relative_to(parent.target)
            )
            if len(parents) != 1 or parents[0][0] >= index or parents[0][1].mask or any(mount.source.iterdir()):
                raise ValueError("sandbox masks require exactly one enclosing non-mask mount and an empty source")
        for other in mounts[index + 1 :]:
            if mount.target == other.target:
                raise ValueError("sandbox mount targets must not overlap")
            if mount.target.is_relative_to(other.target):
                child, parent = mount, other
            elif other.target.is_relative_to(mount.target):
                child, parent = other, mount
            else:
                continue
            if not child.mask or parent.mask:
                raise ValueError("sandbox mount targets must not overlap")


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Bounded facts returned after one isolated command and its cleanup."""

    exit_code: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    duration_seconds: float = 0.0
    output_truncated: bool = False
    backend: str = ""

    def __post_init__(self) -> None:
        """Reject impossible result facts and require the actual backend name."""
        if not self.backend.strip():
            raise ValueError("sandbox result backend must be non-empty")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("sandbox duration must be finite and non-negative")
        if self.timed_out and self.exit_code is not None:
            raise ValueError("timed out sandbox results must not claim an exit code")


class SandboxReceipt(ContractModel):
    """Retain the actual accepted OCI identity for host-owned recovery and cancellation."""

    runtime: Literal["docker", "podman"]
    operation_id: str = Field(min_length=1, max_length=256)
    container_name: str = Field(min_length=1, max_length=256)
    container_id: str = Field(min_length=1, max_length=256)
    admission_token: str = Field(min_length=1, max_length=256)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SandboxObservation(ContractModel):
    """Expose verified daemon facts without interpreting ordinary absence as non-admission."""

    operation_id: str
    state: Literal["created", "running", "succeeded", "failed", "cancelled", "unknown"]
    receipt: SandboxReceipt | None = None
    exit_code: int | None = None
    evidence: JsonObject = Field(default_factory=dict)
