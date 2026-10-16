import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from pleiad.sandbox.policy import SandboxPolicy

MAX_STDIN_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class SandboxMount:
    """One explicitly permitted host-to-container bind mount."""

    source: Path
    target: PurePosixPath
    read_only: bool = True

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

    def __post_init__(self) -> None:
        """Reject ambiguous commands, unsafe paths, and mutable request state."""
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
        targets = tuple(mount.target for mount in self.mounts)
        for index, target in enumerate(targets):
            if any(
                index != other_index
                and (target == other or target.is_relative_to(other) or other.is_relative_to(target))
                for other_index, other in enumerate(targets)
            ):
                raise ValueError("sandbox mount targets must not overlap")
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


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
