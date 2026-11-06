import asyncio
import logging
import re
import shutil
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol
from uuid import uuid4

from pleiad.errors.base import PleiadError
from pleiad.observability.attributes import (
    SANDBOX_BACKEND,
    SANDBOX_CLEANUP_NAME,
    SANDBOX_CREATE_NAME,
    SANDBOX_EXECUTE_NAME,
    SANDBOX_EXIT_CODE,
    SANDBOX_TIMED_OUT,
)
from pleiad.observability.tracing import Observability
from pleiad.sandbox.backends.process import ProcessResult, run_process
from pleiad.sandbox.errors import (
    SandboxCleanupError,
    SandboxConfigurationError,
    SandboxExecutionError,
    SandboxUnavailableError,
)
from pleiad.sandbox.models import SandboxRequest, SandboxResult

logger = logging.getLogger(__name__)

CONTAINER_LABEL_KEY = "com.pleiad.sandbox.owner"
CONTAINER_LABEL_VALUE = "pleiad-sdk"
CONTAINER_NAME_PREFIX = "pleiad-sandbox-"
CONTAINER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
TMPFS_SPEC = "/tmp:rw,noexec,nosuid,size=64m"  # noqa: S108
DIAGNOSTIC_OUTPUT_LIMIT = 64 * 1024
ERROR_DETAIL_LIMIT = 2048


class ProcessExecutor(Protocol):
    """External CLI boundary used by the real process runner and deterministic tests."""

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        stdin: bytes | None,
        output_limit: int,
        timeout_seconds: float | None,
    ) -> Awaitable[ProcessResult]:
        """Execute one OCI CLI command."""


@dataclass(frozen=True, slots=True)
class ContainerIdentity:
    """Unique SDK-owned identity used to scope all container cleanup."""

    name: str
    label_key: str = CONTAINER_LABEL_KEY
    label_value: str = CONTAINER_LABEL_VALUE


@dataclass(slots=True)
class ContainerReference:
    """Container identity whose daemon ID becomes known after create."""

    identity: ContainerIdentity
    container_id: str | None = None


class OciSandbox:
    """Execute one request through an explicitly selected Docker or Podman runtime."""

    def __init__(
        self,
        runtime: str = "docker",
        process_executor: ProcessExecutor | None = None,
        observability: Observability | None = None,
    ) -> None:
        """Select Docker or Podman and optionally replace only the external CLI boundary."""
        if runtime not in {"docker", "podman"}:
            raise SandboxConfigurationError(f"Unsupported OCI runtime: {runtime}.")
        self.runtime = runtime
        self.requires_runtime_binary = process_executor is None
        self.process_executor = run_process if process_executor is None else process_executor
        self.observability = Observability() if observability is None else observability

    def available(self) -> bool:
        """Return whether the explicitly selected OCI executable exists on PATH."""
        return shutil.which(self.runtime) is not None

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        """Create, attach, inspect, and remove one uniquely owned container."""
        with self.observability.span(
            SANDBOX_EXECUTE_NAME,
            attributes={SANDBOX_BACKEND: self.runtime},
        ) as span:
            result = await self.execute_lifecycle(request)
            self.observability.set_attributes(
                span,
                {
                    SANDBOX_TIMED_OUT: result.timed_out,
                    **({SANDBOX_EXIT_CODE: result.exit_code} if result.exit_code is not None else {}),
                },
            )
            self.observability.finish(span, "timed_out" if result.timed_out else "succeeded")
            return result

    async def execute_lifecycle(self, request: SandboxRequest) -> SandboxResult:
        """Own the concrete container lifecycle beneath one Sandbox execution."""
        if self.requires_runtime_binary and not self.available():
            raise SandboxUnavailableError(f"The selected OCI runtime is unavailable: {self.runtime}.")
        started = time.monotonic()
        deadline = started + request.policy.timeout_seconds
        await self.check_runtime(deadline)
        if remaining_seconds(deadline) <= 0:
            return SandboxResult(
                exit_code=None,
                timed_out=True,
                duration_seconds=time.monotonic() - started,
                backend=self.runtime,
            )
        reference = ContainerReference(create_container_identity())
        try:
            result = await self.execute_container(request, reference, started=started, deadline=deadline)
        except asyncio.CancelledError as error:
            try:
                await self.cleanup_after_cancellation(reference, request)
            except SandboxCleanupError as cleanup_error:
                error.add_note(f"Sandbox cleanup failed after cancellation: {cleanup_error.message}")
                logger.error(
                    f"sandbox cleanup failed after cancellation runtime={self.runtime} name={reference.identity.name}",
                    exc_info=True,
                )
            raise
        except Exception as error:
            try:
                await self.cleanup_container(reference, request)
            except SandboxCleanupError as cleanup_error:
                original = error.message if isinstance(error, PleiadError) else type(error).__name__
                raise SandboxCleanupError(f"{original} Cleanup also failed: {cleanup_error.message}") from error
            raise
        await self.cleanup_container(reference, request)
        return result

    async def check_runtime(self, deadline: float) -> None:
        """Check the selected engine before any container can be created.

        Args:
            deadline: The request's absolute monotonic execution deadline.

        Raises:
            SandboxUnavailableError: If the CLI or engine is unavailable or the
                connection check exceeds the request deadline.
        """
        result = await self.execute_cli(
            (self.runtime, "info"),
            stdin=None,
            output_limit=DIAGNOSTIC_OUTPUT_LIMIT,
            timeout_seconds=remaining_seconds(deadline),
        )
        if result.timed_out or result.returncode != 0:
            raise SandboxUnavailableError(
                f"The selected OCI runtime is not responding: {self.runtime}. "
                f"Run '{self.runtime} info' to check the engine and connection."
            )

    async def cleanup_after_cancellation(self, reference: ContainerReference, request: SandboxRequest) -> None:
        """Finish owned cleanup even if the caller cancels the waiting task again."""
        cleanup_task = asyncio.create_task(self.cleanup_container(reference, request))
        while True:
            try:
                await asyncio.shield(cleanup_task)
                return
            except asyncio.CancelledError:
                continue

    async def execute_container(
        self, request: SandboxRequest, reference: ContainerReference, *, started: float, deadline: float
    ) -> SandboxResult:
        """Run the container lifecycle while retaining its confirmed identity."""
        with self.observability.span(
            SANDBOX_CREATE_NAME,
            attributes={SANDBOX_BACKEND: self.runtime},
        ) as create_span:
            create_output = await self.execute_cli(
                build_create_command(request, self.runtime, reference.identity),
                stdin=None,
                output_limit=DIAGNOSTIC_OUTPUT_LIMIT,
                timeout_seconds=remaining_seconds(deadline),
            )
            self.observability.finish(create_span, "timed_out" if create_output.timed_out else "succeeded")
        if create_output.timed_out:
            reference.container_id = parse_optional_container_id(create_output.stdout)
            return sandbox_result_from_process(
                create_output,
                exit_code=None,
                backend=self.runtime,
                duration_seconds=time.monotonic() - started,
                timed_out=True,
                limit=request.policy.max_output_bytes,
            )
        if create_output.returncode != 0:
            raise SandboxExecutionError(
                cli_failure_message("The OCI runtime could not create the Sandbox container.", create_output)
            )
        reference.container_id = parse_container_id(create_output.stdout)

        start_output = await self.execute_cli(
            build_start_command(self.runtime, reference.container_id, request.stdin is not None),
            stdin=request.stdin,
            output_limit=request.policy.max_output_bytes,
            timeout_seconds=remaining_seconds(deadline),
        )
        stdout, stderr, output_truncated = bounded_output(
            start_output.stdout,
            start_output.stderr,
            request.policy.max_output_bytes,
            start_output.output_truncated,
        )
        if start_output.timed_out:
            return SandboxResult(
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                duration_seconds=time.monotonic() - started,
                output_truncated=output_truncated,
                backend=self.runtime,
            )

        inspect_output = await self.execute_cli(
            build_inspect_command(self.runtime, reference.container_id),
            stdin=None,
            output_limit=DIAGNOSTIC_OUTPUT_LIMIT,
            timeout_seconds=remaining_seconds(deadline),
        )
        if inspect_output.timed_out or inspect_output.returncode != 0:
            raise SandboxExecutionError(
                cli_failure_message("The OCI runtime could not inspect the Sandbox exit status.", inspect_output)
            )
        return SandboxResult(
            exit_code=parse_exit_code(inspect_output.stdout),
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started,
            output_truncated=output_truncated,
            backend=self.runtime,
        )

    async def execute_cli(
        self,
        command: tuple[str, ...],
        *,
        stdin: bytes | None,
        output_limit: int,
        timeout_seconds: float | None,
    ) -> ProcessResult:
        """Run one selected-runtime CLI command at the external process boundary."""
        if timeout_seconds is not None and timeout_seconds <= 0:
            return ProcessResult(
                returncode=None,
                stdout=b"",
                stderr=b"",
                timed_out=True,
                duration_seconds=0.0,
                output_truncated=False,
            )
        try:
            return await self.process_executor(
                command,
                stdin=stdin,
                output_limit=output_limit,
                timeout_seconds=timeout_seconds,
            )
        except OSError as error:
            raise SandboxUnavailableError(f"The selected OCI runtime could not execute: {self.runtime}.") from error

    async def cleanup_container(self, reference: ContainerReference, request: SandboxRequest) -> None:
        """Remove only the owned container and verify that the daemon no longer sees it."""
        with self.observability.span(
            SANDBOX_CLEANUP_NAME,
            attributes={SANDBOX_BACKEND: self.runtime},
        ) as cleanup_span:
            await self.cleanup_container_operation(reference, request)
            self.observability.finish(cleanup_span, "succeeded")

    async def cleanup_container_operation(self, reference: ContainerReference, request: SandboxRequest) -> None:
        """Perform the bounded container removal and verification operations."""
        deadline = time.monotonic() + request.policy.cleanup_timeout_seconds
        container_id = reference.container_id
        if container_id is None:
            lookup = await self.cleanup_cli(
                build_lookup_command(self.runtime, reference.identity),
                deadline,
            )
            if lookup.timed_out or lookup.returncode != 0:
                raise SandboxCleanupError("The OCI runtime could not resolve the owned Sandbox container.")
            candidates = parse_container_ids(lookup.stdout)
            if len(candidates) > 1:
                raise SandboxCleanupError("The OCI runtime found multiple containers for one Sandbox identity.")
            if not candidates:
                return
            container_id = candidates[0]
            reference.container_id = container_id

        remove = await self.cleanup_cli(
            build_remove_command(self.runtime, container_id),
            deadline,
        )
        verification = await self.cleanup_cli(
            build_inspect_command(self.runtime, container_id),
            deadline,
        )
        if verification.timed_out:
            raise SandboxCleanupError("The OCI runtime timed out while confirming Sandbox removal.")
        missing = container_missing(verification)
        if remove.timed_out or (remove.returncode != 0 and not missing) or not missing:
            raise SandboxCleanupError("The OCI runtime could not confirm Sandbox removal.")

    async def cleanup_cli(self, command: tuple[str, ...], deadline: float) -> ProcessResult:
        """Run a cleanup command under the independent cleanup deadline."""
        remaining = remaining_seconds(deadline)
        if remaining <= 0:
            raise SandboxCleanupError("The Sandbox cleanup budget was exhausted.")
        try:
            return await self.process_executor(
                command,
                stdin=None,
                output_limit=DIAGNOSTIC_OUTPUT_LIMIT,
                timeout_seconds=remaining,
            )
        except OSError as error:
            raise SandboxCleanupError("The OCI runtime could not execute the Sandbox cleanup command.") from error


def cli_failure_message(message: str, result: ProcessResult) -> str:
    """Append bounded CLI stderr without serializing commands or environment values."""
    detail = result.stderr[:ERROR_DETAIL_LIMIT].decode("utf-8", errors="replace").strip()
    if not detail:
        return message
    suffix = " [truncated]" if len(result.stderr) > ERROR_DETAIL_LIMIT else ""
    return f"{message} Runtime stderr: {detail}{suffix}"


def create_container_identity() -> ContainerIdentity:
    """Create a unique name and fixed SDK ownership label for one request."""
    return ContainerIdentity(name=f"{CONTAINER_NAME_PREFIX}{uuid4().hex}")


def build_create_command(request: SandboxRequest, runtime: str, identity: ContainerIdentity) -> tuple[str, ...]:
    """Build fixed-security OCI create arguments with an explicit entrypoint."""
    entrypoint, arguments = request_entrypoint(request)
    command: list[str] = [
        runtime,
        "create",
        "--name",
        identity.name,
        "--label",
        f"{identity.label_key}={identity.label_value}",
        "--label",
        f"{identity.label_key}.request={identity.name}",
        "--pull=never",
        "--init",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        request.policy.user,
        "--pids-limit",
        str(request.policy.pids_limit),
        "--memory",
        str(request.policy.memory_bytes),
        "--cpus",
        str(request.policy.cpus),
        "--tmpfs",
        TMPFS_SPEC,
        "--log-driver",
        "none",
        "--workdir",
        str(request.cwd),
    ]
    if request.stdin is not None:
        command.append("--interactive")
    for mount in request.mounts:
        command.extend(("--mount", mount_argument(mount.source.as_posix(), mount.target, mount.read_only)))
    for key, value in sorted(request.environment.items()):
        command.extend(("--env", f"{key}={value}"))
    command.extend(("--entrypoint", entrypoint, request.policy.image))
    command.extend(arguments)
    return tuple(command)


def build_start_command(runtime: str, container_id: str, interactive: bool) -> tuple[str, ...]:
    """Build an attach command scoped to one daemon-returned container ID."""
    command = [runtime, "start", "--attach"]
    if interactive:
        command.append("--interactive")
    command.append(container_id)
    return tuple(command)


def build_inspect_command(runtime: str, container_id: str) -> tuple[str, ...]:
    """Build an exact container exit-status inspection command."""
    return (runtime, "inspect", "--type", "container", "--format", "{{.State.ExitCode}}", container_id)


def build_lookup_command(runtime: str, identity: ContainerIdentity) -> tuple[str, ...]:
    """Build an exact name-and-owner lookup used only when create returned no ID."""
    return (
        runtime,
        "container",
        "ls",
        "--all",
        "--quiet",
        "--filter",
        f"name=^/{identity.name}$",
        "--filter",
        f"label={identity.label_key}={identity.label_value}",
    )


def build_remove_command(runtime: str, container_id: str) -> tuple[str, ...]:
    """Build a force removal command scoped to one confirmed container ID."""
    return (runtime, "rm", "--force", container_id)


def request_entrypoint(request: SandboxRequest) -> tuple[str, tuple[str, ...]]:
    """Resolve the explicit argv or the authorized bash entrypoint."""
    if request.shell_command is not None:
        return "bash", ("--noprofile", "--norc", "-c", request.shell_command)
    if request.argv is None:
        raise ValueError("sandbox request has no command")
    return request.argv[0], request.argv[1:]


def mount_argument(source: str, target: PurePosixPath, read_only: bool) -> str:
    """Build one bind mount argument without accepting arbitrary runtime flags."""
    suffix = ",readonly" if read_only else ""
    return f"type=bind,src={source},dst={target}{suffix}"


def parse_container_id(value: bytes) -> str:
    """Parse the single daemon identity returned by create."""
    candidates = parse_container_ids(value)
    if len(candidates) != 1:
        raise SandboxExecutionError("The OCI runtime returned an invalid Sandbox container identity.")
    return candidates[0]


def parse_optional_container_id(value: bytes) -> str | None:
    """Recover a daemon identity from partial create output when possible."""
    candidates = parse_container_ids(value)
    return candidates[0] if len(candidates) == 1 else None


def parse_container_ids(value: bytes) -> tuple[str, ...]:
    """Parse safe daemon IDs from line-oriented CLI output."""
    try:
        lines = value.decode("ascii").split()
    except UnicodeDecodeError:
        return ()
    return tuple(line for line in lines if CONTAINER_ID_PATTERN.fullmatch(line))


def parse_exit_code(value: bytes) -> int:
    """Parse the exact integer exit code returned by inspect."""
    try:
        text = value.decode("ascii").strip()
        exit_code = int(text)
    except (UnicodeDecodeError, ValueError) as error:
        raise SandboxExecutionError("The OCI runtime returned an invalid Sandbox exit status.") from error
    if exit_code < 0:
        raise SandboxExecutionError("The OCI runtime returned a negative Sandbox exit status.")
    return exit_code


def bounded_output(
    stdout: bytes,
    stderr: bytes,
    limit: int,
    already_truncated: bool,
) -> tuple[bytes, bytes, bool]:
    """Keep each output stream within its independent policy limit."""
    bounded_stdout = stdout[:limit]
    bounded_stderr = stderr[:limit]
    truncated = already_truncated or len(bounded_stdout) < len(stdout) or len(bounded_stderr) < len(stderr)
    return bounded_stdout, bounded_stderr, truncated


def sandbox_result_from_process(
    process: ProcessResult,
    *,
    exit_code: int | None,
    backend: str,
    duration_seconds: float,
    timed_out: bool,
    limit: int,
) -> SandboxResult:
    """Project a bounded CLI fact into the public Sandbox result contract."""
    stdout, stderr, output_truncated = bounded_output(
        process.stdout,
        process.stderr,
        limit,
        process.output_truncated,
    )
    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_seconds=duration_seconds,
        output_truncated=output_truncated,
        backend=backend,
    )


def container_missing(result: ProcessResult) -> bool:
    """Recognize only explicit daemon not-found responses as removal confirmation."""
    if result.returncode == 0:
        return False
    diagnostic = result.stderr.decode("utf-8", errors="replace").casefold()
    return "no such object" in diagnostic or "no such container" in diagnostic or "not found" in diagnostic


def remaining_seconds(deadline: float) -> float:
    """Return the remaining budget, using zero for an exhausted deadline."""
    return max(0.0, deadline - time.monotonic())
