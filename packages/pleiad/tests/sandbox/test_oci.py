import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.sandbox.errors import (
    SandboxCancelledError,
    SandboxCleanupError,
    SandboxExecutionError,
    SandboxUnavailableError,
)
from pleiad.sandbox.models import SandboxReceipt, SandboxRequest
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
        self.timeouts: list[float | None] = []

    async def __call__(
        self,
        command: tuple[str, ...],
        *,
        stdin: bytes | None,
        output_limit: int,
        timeout_seconds: float | None,
    ) -> ProcessResult:
        """Record one command and ask the deterministic daemon script for its fact."""
        del output_limit
        self.commands.append(command)
        self.inputs.append(stdin)
        self.timeouts.append(timeout_seconds)
        return await self.responses(command, stdin)


def lifecycle_response(
    exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> Callable[[tuple[str, ...], bytes | None], Awaitable[ProcessResult]]:
    """Create a normal create/start/inspect/remove daemon script."""
    removed = False

    async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
        del stdin
        nonlocal removed
        if command[1] == "info":
            return ProcessResult(0, b"", b"", False, 0.01, False)
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
        assert [command[1] for command in executor.commands] == ["info", "create", "start", "inspect", "rm", "inspect"]
        assert executor.inputs[2] == b"input"
        create = executor.commands[1]
        assert "--entrypoint" in create
        assert "--network" in create
        assert create[create.index("--network") + 1] == "none"
        assert "--read-only" in create
        assert "--cap-drop" in create
        assert "--security-opt" in create
        assert "--user" in create

    asyncio.run(scenario())


@pytest.mark.parametrize("runtime", ["docker", "podman"])
@pytest.mark.parametrize("payload", [b'{"value":"input"}', b"", None], ids=["json", "empty", "absent"])
def test_oci_opens_container_stdin_before_attaching_request_payloads(runtime: str, payload: bytes | None) -> None:
    async def scenario() -> None:
        stdin_open = False
        normal_response = lifecycle_response()

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            nonlocal stdin_open
            if command[1] == "create":
                stdin_open = "--interactive" in command
            if command[1] == "start":
                received = stdin if stdin_open and "--interactive" in command and stdin is not None else b""
                return ProcessResult(0, received, b"", False, 0.01, False)
            return await normal_response(command, stdin)

        request = SandboxRequest(
            cwd=PurePosixPath("/pleiad"),
            policy=make_policy(max_output_bytes=64),
            shell_command="cat",
            stdin=payload,
        )
        executor = ScriptedExecutor(respond)
        result = await OciSandbox(runtime=runtime, process_executor=executor).execute(request)

        assert result.exit_code == 0
        assert result.stdout == (payload if payload is not None else b"")
        assert ("--interactive" in executor.commands[1]) is (payload is not None)
        assert ("--interactive" in executor.commands[2]) is (payload is not None)
        assert executor.inputs == [None, None, payload, None, None, None]

    asyncio.run(scenario())


def test_oci_timeout_returns_bounded_output_after_removal(tmp_path: Path) -> None:
    async def scenario() -> None:
        removed = False

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            del stdin
            nonlocal removed
            if command[1] == "info":
                return ProcessResult(0, b"", b"", False, 0.01, False)
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
        assert [command[1] for command in executor.commands] == ["info", "create", "start", "rm", "inspect"]

    asyncio.run(scenario())


def test_oci_cancellation_cleans_owned_container_and_propagates_cancel(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        removed = False

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            del stdin
            nonlocal removed
            if command[1] == "info":
                return ProcessResult(0, b"", b"", False, 0.01, False)
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
        assert [command[1] for command in executor.commands] == ["info", "create", "start", "rm", "inspect"]

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
            if command[1] == "info":
                return ProcessResult(0, b"", b"", False, 0.01, False)
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
            if command[1] == "info":
                return ProcessResult(0, b"", b"", False, 0.01, False)
            if command[1] == "create":
                return ProcessResult(125, b"", b"image not present", False, 0.01, False)
            if command[1] == "container":
                return ProcessResult(0, b"", b"", False, 0.01, False)
            raise AssertionError(f"unexpected command: {command}")

        executor = ScriptedExecutor(respond)
        with pytest.raises(SandboxExecutionError) as caught:
            await OciSandbox(process_executor=executor).execute(make_request(tmp_path))

        assert "image not present" in caught.value.message
        assert [command[1] for command in executor.commands] == ["info", "create", "container"]
        assert "--filter" in executor.commands[2]
        assert "label=com.pleiad.sandbox.owner=pleiad-sdk" in executor.commands[2]

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["create", "inspect"])
def test_oci_runtime_failure_reports_bounded_stderr_without_command_data(stage: str) -> None:
    async def scenario() -> None:
        normal_response = lifecycle_response(stdout=b"private command output")
        failed = False

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            nonlocal failed
            if command[1] == stage and not failed:
                failed = True
                return ProcessResult(
                    125,
                    b"private command output",
                    b"daemon diagnosis: \xff " + b"x" * 3000 + b"tail",
                    False,
                    0.01,
                    False,
                )
            if command[1] == "container":
                return ProcessResult(0, b"", b"", False, 0.01, False)
            return await normal_response(command, stdin)

        request = SandboxRequest(
            cwd=PurePosixPath("/pleiad"),
            policy=SandboxPolicy(image="python@sha256:" + "a" * 64, allowed_environment=frozenset({"TOKEN"})),
            argv=("python", "private-argument"),
            environment={"TOKEN": "private-token"},
            stdin=b"private-stdin",
        )
        executor = ScriptedExecutor(respond)
        with pytest.raises(SandboxExecutionError) as caught:
            await OciSandbox(process_executor=executor).execute(request)

        message = caught.value.message
        assert caught.value.code == "sandbox.execution.failed"
        assert "daemon diagnosis: \ufffd" in message
        assert message.endswith(" [truncated]")
        assert "tail" not in message
        assert "private" not in message
        assert len(message) < 2300

    asyncio.run(scenario())


@pytest.mark.parametrize("runtime", ["docker", "podman"])
@pytest.mark.parametrize("timed_out", [False, True])
def test_oci_unreachable_engine_fails_before_container_creation(tmp_path: Path, runtime: str, timed_out: bool) -> None:
    async def scenario() -> None:
        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            if command != (runtime, "info"):
                raise AssertionError(f"unexpected container operation: {command}")
            return ProcessResult(None if timed_out else 125, b"", b"private socket path", timed_out, 0.01, False)

        executor = ScriptedExecutor(respond)
        with pytest.raises(SandboxUnavailableError) as caught:
            await OciSandbox(runtime=runtime, process_executor=executor).execute(make_request(tmp_path))

        assert caught.value.code == "sandbox.runtime.unavailable"
        assert f"{runtime} info" in caught.value.message
        assert "private socket path" not in caught.value.message
        assert executor.commands == [(runtime, "info")]

    asyncio.run(scenario())


def test_oci_cancelled_connection_check_propagates_cancel_without_container_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            if command != ("docker", "info"):
                raise AssertionError(f"unexpected container operation: {command}")
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("connection check should be cancelled")

        executor = ScriptedExecutor(respond)
        task = asyncio.create_task(OciSandbox(process_executor=executor).execute(make_request(tmp_path)))
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert executor.commands == [("docker", "info")]

    asyncio.run(scenario())


def test_oci_connection_check_consumes_the_existing_execution_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr("pleiad.sandbox.backends.oci.time", SimpleNamespace(monotonic=lambda: now))

    async def scenario() -> None:
        normal_response = lifecycle_response()

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            nonlocal now
            if command[1] == "info":
                now += 2.0
            return await normal_response(command, stdin)

        request = make_request(tmp_path)
        executor = ScriptedExecutor(respond)
        result = await OciSandbox(process_executor=executor).execute(request)

        assert result.duration_seconds == 2.0
        assert executor.timeouts[:2] == [request.policy.timeout_seconds, request.policy.timeout_seconds - 2.0]

    asyncio.run(scenario())


@pytest.mark.parametrize("overrun", [0.0, 1.0])
def test_oci_expired_connection_check_returns_timeout_without_starting_a_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrun: float
) -> None:
    now = 100.0
    monkeypatch.setattr("pleiad.sandbox.backends.oci.time", SimpleNamespace(monotonic=lambda: now))

    async def scenario() -> None:
        request = make_request(tmp_path)

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            nonlocal now
            if command != ("docker", "info"):
                raise AssertionError(f"unexpected container operation: {command}")
            now += request.policy.timeout_seconds + overrun
            return ProcessResult(0, b"", b"", False, 0.01, False)

        executor = ScriptedExecutor(respond)
        result = await OciSandbox(process_executor=executor).execute(request)

        assert result.timed_out is True
        assert result.exit_code is None
        assert result.duration_seconds == request.policy.timeout_seconds + overrun
        assert executor.commands == [("docker", "info")]

    asyncio.run(scenario())


def test_oci_deadline_reached_after_create_skips_start_and_preserves_the_cleanup_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr("pleiad.sandbox.backends.oci.time", SimpleNamespace(monotonic=lambda: now))

    async def scenario() -> None:
        request = make_request(tmp_path)
        normal_response = lifecycle_response()

        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            nonlocal now
            if command[1] == "create":
                now += request.policy.timeout_seconds
            return await normal_response(command, stdin)

        executor = ScriptedExecutor(respond)
        result = await OciSandbox(process_executor=executor).execute(request)

        assert result.timed_out is True
        assert result.exit_code is None
        assert [command[1] for command in executor.commands] == ["info", "create", "rm", "inspect"]
        assert executor.commands[2][-1] == "container-id"
        assert executor.timeouts[2:] == [request.policy.cleanup_timeout_seconds] * 2

    asyncio.run(scenario())


def test_oci_connection_loss_during_create_preserves_execution_and_cleanup_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def respond(command: tuple[str, ...], stdin: bytes | None) -> ProcessResult:
            if command[1] == "info":
                return ProcessResult(0, b"", b"", False, 0.01, False)
            if command[1] in {"create", "container"}:
                return ProcessResult(125, b"", b"daemon unavailable", False, 0.01, False)
            raise AssertionError(f"unexpected container operation: {command}")

        executor = ScriptedExecutor(respond)
        with pytest.raises(SandboxCleanupError) as caught:
            await OciSandbox(process_executor=executor).execute(make_request(tmp_path))

        assert caught.value.code == "sandbox.cleanup.failed"
        assert "could not create the Sandbox container" in caught.value.message
        assert "could not resolve the owned Sandbox container" in caught.value.message
        assert isinstance(caught.value.__cause__, SandboxExecutionError)
        assert [command[1] for command in executor.commands] == ["info", "create", "container"]
        assert "label=com.pleiad.sandbox.owner=pleiad-sdk" in executor.commands[-1]

    asyncio.run(scenario())


def test_oci_unavailable_does_not_run_a_host_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        sandbox = OciSandbox()

        with pytest.raises(SandboxUnavailableError):
            await sandbox.execute(make_request(tmp_path))

    monkeypatch.setattr("pleiad.sandbox.backends.oci.shutil.which", lambda runtime: None)
    asyncio.run(scenario())


class ActionDaemon:
    """Persist fake external daemon identities independently of SDK backend instances."""

    def __init__(self) -> None:
        self.exists = False
        self.exit_code: int | None = None
        self.name = ""
        self.labels: dict[str, str] = {}
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.commands: list[tuple[str, ...]] = []

    async def __call__(
        self, command: tuple[str, ...], *, stdin: bytes | None, output_limit: int, timeout_seconds: float | None
    ) -> ProcessResult:
        self.commands.append(command)
        if command[1] == "info":
            return ProcessResult(0, b"", b"", False, 0.0, False)
        if command[1] == "create":
            if self.exists:
                return ProcessResult(1, b"", b"Container name is already in use", False, 0.0, False)
            self.exists = True
            self.name = command[command.index("--name") + 1]
            self.labels = dict(
                command[index + 1].split("=", 1) for index, value in enumerate(command) if value == "--label"
            )
            return ProcessResult(0, b"accepted-container", b"", False, 0.0, False)
        if command[1] == "start":
            self.started.set()
            await self.release.wait()
            return ProcessResult(0, b"completed", b"", False, 0.0, False)
        if command[1] == "container":
            token = self.labels.get("com.pleiad.sandbox.owner.request")
            matches = self.exists and f"label=com.pleiad.sandbox.owner.request={token}" in command
            return ProcessResult(0, b"accepted-container" if matches else b"", b"", False, 0.0, False)
        if command[1] == "rm":
            self.exists = False
            self.release.set()
            return ProcessResult(0, b"accepted-container", b"", False, 0.0, False)
        if command[1] == "inspect":
            if not self.exists:
                return ProcessResult(1, b"", b"No such container", False, 0.0, False)
            if "{{json .}}" in command:
                payload = {
                    "Id": "accepted-container",
                    "Name": f"/{self.name}",
                    "Config": {"Labels": self.labels},
                    "State": {
                        "Status": "exited"
                        if self.exit_code is not None
                        else ("running" if self.started.is_set() else "created"),
                        "Running": self.exit_code is None and self.started.is_set(),
                        "ExitCode": 0 if self.exit_code is None else self.exit_code,
                    },
                }
                return ProcessResult(0, json.dumps(payload).encode(), b"", False, 0.0, False)
            return ProcessResult(0, b"0", b"", False, 0.0, False)
        raise AssertionError(f"Unexpected daemon request {command}")


def test_oci_acceptance_is_queryable_before_execution_and_duplicate_admission_cannot_remove_it() -> None:
    """Stable backend identity survives a new SDK instance and failed duplicate creation."""

    async def scenario() -> None:
        daemon = ActionDaemon()
        backend = OciSandbox(process_executor=daemon)
        accepted = asyncio.Event()
        release = asyncio.Event()
        receipts: list[SandboxReceipt] = []

        async def on_accept(receipt: SandboxReceipt) -> None:
            receipts.append(receipt)
            accepted.set()
            await release.wait()

        request = SandboxRequest(
            cwd=PurePosixPath("/"),
            policy=make_policy(),
            shell_command="printf accepted",
            operation_id="durable-operation",
        )
        async with asyncio.TaskGroup() as group:
            task = group.create_task(backend.execute(request, on_accept=on_accept))
            await asyncio.wait_for(accepted.wait(), timeout=3)
            assert not daemon.started.is_set()
            restarted = OciSandbox(process_executor=daemon)
            observed = await restarted.query("durable-operation")
            assert observed.state == "created" and observed.receipt == receipts[0]
            assert receipts[0].request_digest == request.digest()
            with pytest.raises(SandboxExecutionError, match="already in use"):
                await restarted.execute(request, on_accept=on_accept)
            assert daemon.exists
            assert not any(command[1] == "rm" for command in daemon.commands)
            stopped = await restarted.stop(receipts[0])
            assert stopped.state == "cancelled" and stopped.evidence == {"container_removed": True}
            missing = await restarted.query("durable-operation")
            assert missing.state == "unknown" and missing.receipt is None
            assert (await restarted.stop(receipts[0])).state == "unknown"
            task.cancel()
            with pytest.raises(SandboxCancelledError) as cancelled:
                await task
            assert cancelled.value.confirmed_stop

    asyncio.run(scenario())


def test_oci_query_cannot_adopt_a_foreign_container_at_the_stable_operation_name() -> None:
    """Host recovery verifies backend ownership before it exposes an accepted receipt."""

    async def scenario() -> None:
        daemon = ActionDaemon()
        backend = OciSandbox(process_executor=daemon)
        accepted = asyncio.Event()
        release = asyncio.Event()

        async def on_accept(receipt: SandboxReceipt) -> None:
            accepted.set()
            await release.wait()

        request = SandboxRequest(
            cwd=PurePosixPath("/"), policy=make_policy(), shell_command="true", operation_id="owned"
        )
        async with asyncio.TaskGroup() as group:
            task = group.create_task(backend.execute(request, on_accept=on_accept))
            await asyncio.wait_for(accepted.wait(), timeout=3)
            daemon.labels["com.pleiad.sandbox.owner"] = "foreign"
            observation = await OciSandbox(process_executor=daemon).query("owned")
            assert observation.state == "unknown" and observation.receipt is None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_code", [0, 23], ids=["successful-process", "failed-process"])
def test_stop_retains_known_exit_facts_when_the_process_finishes_after_the_callers_query(exit_code: int) -> None:
    """Cancellation cleanup preserves a process outcome confirmed by stop's fresh inspection."""

    async def scenario() -> None:
        daemon = ActionDaemon()
        accepted: list[SandboxReceipt] = []

        async def on_accept(receipt: SandboxReceipt) -> None:
            accepted.append(receipt)

        request = SandboxRequest(
            cwd=PurePosixPath("/"), policy=make_policy(), shell_command="work", operation_id="finishes-before-stop"
        )
        async with asyncio.TaskGroup() as group:
            task = group.create_task(OciSandbox(process_executor=daemon).execute(request, on_accept=on_accept))
            await asyncio.wait_for(daemon.started.wait(), timeout=3)
            recovery = OciSandbox(process_executor=daemon)
            previous = await recovery.query("finishes-before-stop")
            assert previous.state == "running" and previous.receipt == accepted[0]
            daemon.exit_code = exit_code
            stopped = await recovery.stop(accepted[0])
            assert stopped.state == ("succeeded" if exit_code == 0 else "failed")
            assert stopped.exit_code == exit_code and stopped.receipt == accepted[0]
            assert stopped.evidence == {"container_removed": True} and not daemon.exists
            task.cancel()
            with pytest.raises(SandboxCancelledError):
                await task

    asyncio.run(scenario())
