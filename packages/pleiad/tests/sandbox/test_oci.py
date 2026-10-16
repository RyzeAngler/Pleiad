import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

import pytest

from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.sandbox.errors import SandboxExecutionError, SandboxUnavailableError
from pleiad.sandbox.models import SandboxRequest
from pleiad.sandbox.policy import SandboxPolicy


def make_policy(*, max_output_bytes: int = 8) -> SandboxPolicy:
    """Build a valid OCI policy for lifecycle tests."""
    return SandboxPolicy(
        image="python@sha256:" + "a" * 64,
        allow_shell_command=True,
        max_output_bytes=max_output_bytes,
    )


def make_request(tmp_path: Path, *, stdin: bytes | None = None) -> SandboxRequest:
    """Build one command request with explicit workspace and scratch mounts."""
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    return SandboxRequest(
        cwd=PurePosixPath("/pleiad"),
        policy=make_policy(),
        shell_command="cat",
        stdin=stdin,
        mounts=(),
    )


class ScriptedExecutor:
    """Return scripted daemon facts and record every external CLI request."""

    def __init__(self, responses: Callable[[tuple[str, ...], bytes | None], Awaitable[ProcessResult]]) -> None:
        self.responses = responses
        self.commands: list[tuple[str, ...]] = []
        self.inputs: list[bytes | None] = []

    async def __call__(
        self,
        command: tuple[str, ...],
        *,
        stdin: bytes | None,
        output_limit: int,
        timeout_seconds: float | None,
    ) -> ProcessResult:
        """Record one command and ask the deterministic daemon script for its fact."""
        del output_limit, timeout_seconds
        self.commands.append(command)
        self.inputs.append(stdin)
        return await self.responses(command, stdin)


def lifecycle_response(
    exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> Callable[[tuple[str, ...], bytes | None], Awaitable[ProcessResult]]:
    """Create a normal create/start/inspect/remove daemon script."""
    removed = False

    async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
        del stdin
        nonlocal removed
        if command[1] == "create":
            return ProcessResult(0, b"container-id\n", b"", False, 0.01, False)
        if command[1] == "start":
            return ProcessResult(exit_code, stdout, stderr, False, 0.01, False)
        if command[1] == "inspect" and removed:
            return ProcessResult(1, b"", b"Error: No such object\n", False, 0.01, False)
        if command[1] == "inspect":
            return ProcessResult(0, str(exit_code).encode(), b"", False, 0.01, False)
        if command[1] == "rm":
            removed = True
            return ProcessResult(0, b"container-id\n", b"", False, 0.01, False)
        raise AssertionError(f"unexpected command: {command}")

    return respond


def test_oci_preserves_nonzero_exit_and_uses_inspected_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor = ScriptedExecutor(lifecycle_response(23, b"out", b"err"))
        sandbox = OciSandbox(process_executor=executor)

        result = await sandbox.execute(make_request(tmp_path, stdin=b"input"))

        assert result.exit_code == 23
        assert result.stdout == b"out"
        assert result.stderr == b"err"
        assert result.backend == "docker"
        assert [command[1] for command in executor.commands] == ["create", "start", "inspect", "rm", "inspect"]
        assert executor.inputs[1] == b"input"
        assert "--entrypoint" in executor.commands[0]
        assert "--network" in executor.commands[0]
        assert executor.commands[0][executor.commands[0].index("--network") + 1] == "none"
        assert "--read-only" in executor.commands[0]
        assert "--cap-drop" in executor.commands[0]
        assert "--security-opt" in executor.commands[0]
        assert "--user" in executor.commands[0]

    asyncio.run(scenario())


def test_oci_timeout_returns_bounded_output_after_removal(tmp_path: Path) -> None:
    async def scenario() -> None:
        removed = False

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            del stdin
            nonlocal removed
            if command[1] == "create":
                return ProcessResult(0, b"container-id\n", b"", False, 0.01, False)
            if command[1] == "start":
                return ProcessResult(None, b"o" * 20, b"e" * 20, True, 0.01, True)
            if command[1] == "rm":
                removed = True
                return ProcessResult(0, b"", b"", False, 0.01, False)
            if command[1] == "inspect" and removed:
                return ProcessResult(1, b"", b"No such object", False, 0.01, False)
            raise AssertionError(f"unexpected command: {command}")

        executor = ScriptedExecutor(respond)
        result = await OciSandbox(process_executor=executor).execute(make_request(tmp_path))

        assert result.timed_out is True
        assert result.exit_code is None
        assert result.stdout == b"o" * 8
        assert result.stderr == b"e" * 8
        assert result.output_truncated is True
        assert [command[1] for command in executor.commands] == ["create", "start", "rm", "inspect"]

    asyncio.run(scenario())


def test_oci_cancellation_cleans_owned_container_and_propagates_cancel(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        removed = False

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            del stdin
            nonlocal removed
            if command[1] == "create":
                return ProcessResult(0, b"container-id\n", b"", False, 0.01, False)
            if command[1] == "start":
                started.set()
                await release.wait()
                return ProcessResult(0, b"", b"", False, 0.01, False)
            if command[1] == "rm":
                removed = True
                return ProcessResult(0, b"", b"", False, 0.01, False)
            if command[1] == "inspect" and removed:
                return ProcessResult(1, b"", b"No such object", False, 0.01, False)
            raise AssertionError(f"unexpected command: {command}")

        executor = ScriptedExecutor(respond)
        task = asyncio.create_task(OciSandbox(process_executor=executor).execute(make_request(tmp_path)))
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert [command[1] for command in executor.commands] == ["create", "start", "rm", "inspect"]

    asyncio.run(scenario())


def test_oci_concurrent_cancellation_only_removes_the_cancelled_container(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    async def scenario() -> None:
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        second_release = asyncio.Event()
        created = 0
        removed: set[str] = set()

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            del stdin
            nonlocal created
            if command[1] == "create":
                created += 1
                return ProcessResult(0, f"container-{created}\n".encode(), b"", False, 0.01, False)
            if command[1] == "start" and command[-1] == "container-1":
                first_started.set()
                await asyncio.Event().wait()
            if command[1] == "start" and command[-1] == "container-2":
                second_started.set()
                await second_release.wait()
                return ProcessResult(0, b"second", b"", False, 0.01, False)
            if command[1] == "inspect" and command[-1] in removed:
                return ProcessResult(1, b"", b"No such object", False, 0.01, False)
            if command[1] == "inspect":
                return ProcessResult(0, b"0", b"", False, 0.01, False)
            if command[1] == "rm":
                removed.add(command[-1])
                return ProcessResult(0, b"", b"", False, 0.01, False)
            raise AssertionError(f"unexpected command: {command}")

        executor = ScriptedExecutor(respond)
        first = asyncio.create_task(OciSandbox(process_executor=executor).execute(make_request(first_root)))
        second = asyncio.create_task(OciSandbox(process_executor=executor).execute(make_request(second_root)))
        await first_started.wait()
        await second_started.wait()
        first.cancel()

        with pytest.raises(asyncio.CancelledError):
            await first
        second_release.set()
        result = await second

        assert result.stdout == b"second"
        assert removed == {"container-1", "container-2"}

    asyncio.run(scenario())


def test_oci_create_failure_without_id_reports_failure_and_scoped_lookup(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            del stdin
            if command[1] == "create":
                return ProcessResult(125, b"", b"daemon unavailable", False, 0.01, False)
            if command[1] == "container":
                return ProcessResult(0, b"", b"", False, 0.01, False)
            raise AssertionError(f"unexpected command: {command}")

        executor = ScriptedExecutor(respond)
        with pytest.raises(SandboxExecutionError):
            await OciSandbox(process_executor=executor).execute(make_request(tmp_path))

        assert [command[1] for command in executor.commands] == ["create", "container"]
        assert "--filter" in executor.commands[1]
        assert "label=com.pleiad.sandbox.owner=pleiad-sdk" in executor.commands[1]

    asyncio.run(scenario())


def test_oci_unavailable_does_not_run_a_host_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        sandbox = OciSandbox()

        with pytest.raises(SandboxUnavailableError):
            await sandbox.execute(make_request(tmp_path))

    monkeypatch.setattr("pleiad.sandbox.backends.oci.shutil.which", lambda runtime: None)
    asyncio.run(scenario())
