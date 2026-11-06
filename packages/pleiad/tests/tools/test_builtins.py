import asyncio
import fcntl
import hashlib
import multiprocessing
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection
from multiprocessing.synchronize import Event
from pathlib import Path
from threading import Event as ThreadEvent

import pytest

from pleiad.contracts import CapabilityGrants, JsonObject
from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.tools.builtins.defaults import install_default_tools
from pleiad.tools.builtins.file_support import FileToolBinding
from pleiad.tools.builtins.read_file import ReadFileArguments
from pleiad.tools.builtins.write_file import WriteFileTool
from pleiad.tools.contracts import ToolCall, ToolContext, ToolDescriptor, ToolResult
from pleiad.tools.errors import ToolErrorCode, ToolFileAccessDeniedError
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime


class RecordingExecutor:
    """Capture OCI CLI requests at the external process boundary."""

    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.commands: list[tuple[str, ...]] = []
        self.removed = False

    async def __call__(
        self,
        command: tuple[str, ...],
        *,
        stdin: bytes | None,
        output_limit: int,
        timeout_seconds: float | None,
    ) -> ProcessResult:
        """Return deterministic daemon facts without starting an external process."""
        del stdin, output_limit, timeout_seconds
        self.commands.append(command)
        if command[1] == "info":
            return ProcessResult(0, b"", b"", False, 0.01, False)
        if command[1] == "create":
            return ProcessResult(0, b"container-id\n", b"", False, 0.01, False)
        if command[1] == "start":
            return self.result
        if command[1] == "rm":
            self.removed = True
            return ProcessResult(0, b"container-id\n", b"", False, 0.01, False)
        if command[1] == "inspect" and self.removed:
            return ProcessResult(1, b"", b"Error: No such object\n", False, 0.01, False)
        if command[1] == "inspect":
            return ProcessResult(0, f"{self.result.returncode}\n".encode(), b"", False, 0.01, False)
        raise AssertionError(f"unexpected OCI command: {command}")


def create_bindings(
    tmp_path: Path,
    *,
    include_skill: bool = True,
) -> tuple[ToolRegistry, Path, Path, RecordingExecutor]:
    """Create host roots, optional read-only roots, and a fake Sandbox boundary."""
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    skill_root = tmp_path / "demo"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\nUse the demo skill.\n",
        encoding="utf-8",
    )
    backend = RecordingExecutor(
        ProcessResult(
            returncode=0,
            stdout=b"sandbox output",
            stderr=b"",
            timed_out=False,
            duration_seconds=0.25,
            output_truncated=False,
        )
    )
    registry = ToolRegistry()
    install_default_tools(
        registry,
        workspace=workspace,
        scratch=scratch,
        skill_roots={"demo": skill_root} if include_skill else {},
        sandbox=Sandbox(OciSandbox(process_executor=backend)),
        sandbox_policy=SandboxPolicy(
            image=f"python@sha256:{'a' * 64}",
            allow_shell_command=True,
        ),
    )
    return registry, workspace, scratch, backend


def invoke(
    registry: ToolRegistry,
    code: str,
    arguments: JsonObject,
    *,
    grants: frozenset[str],
) -> ToolResult:
    """Run one built-in Tool through the public ToolRuntime boundary."""
    outcome = asyncio.run(
        ToolRuntime().execute(
            registry.load(code),
            ToolCall(call_id="call-1", tool_code=code, arguments=arguments),
            ToolContext(call_id="call-1", grants=CapabilityGrants(capabilities=grants)),
        )
    )
    assert isinstance(outcome, ToolResult)
    return outcome


@contextmanager
def filesystem_audit(action: Callable[[str, tuple[object, ...]], None]) -> Iterator[None]:
    """Inject filesystem races through Python's OS audit boundary."""
    active = True

    def hook(event: str, arguments: tuple[object, ...]) -> None:
        if active:
            action(event, arguments)

    sys.addaudithook(hook)
    try:
        yield
    finally:
        active = False


def replacement_process(workspace: Path, scratch: Path, locking: Event, result: Connection) -> None:
    """Try one conditional SDK write while another process owns its directory lock."""
    binding = FileToolBinding(workspace, scratch)
    tool = WriteFileTool(ToolDescriptor(name="write_file", code="write_file", description="Write"), binding)

    def signal_lock_attempt(event: str, arguments: tuple[object, ...]) -> None:
        if event == "fcntl.flock" and arguments[1] == fcntl.LOCK_EX | fcntl.LOCK_NB:
            locking.set()

    with filesystem_audit(signal_lock_attempt):
        outcome = asyncio.run(
            ToolRuntime().execute(
                tool,
                ToolCall(
                    call_id="writer",
                    tool_code="write_file",
                    arguments={
                        "path": "result.txt",
                        "content": "SDK replacement",
                        "mode": "replace",
                        "expected_sha256": hashlib.sha256(b"original").hexdigest(),
                    },
                ),
                ToolContext(call_id="writer", grants=CapabilityGrants(capabilities=frozenset({"file.write"}))),
            )
        )
    result.send(outcome.model_dump(mode="json"))
    result.close()


def test_default_registry_exposes_the_three_builtin_tools(tmp_path: Path) -> None:
    """A host installation makes read_file, write_file, and command resolvable."""
    registry, _, _, _ = create_bindings(tmp_path)

    assert registry.codes() == ("command", "read_file", "write_file")
    assert registry.load("read_file").descriptor.name == "read_file"
    assert registry.load("write_file").descriptor.name == "write_file"
    assert registry.load("command").descriptor.name == "command"


def test_read_file_returns_the_requested_workspace_line_window(tmp_path: Path) -> None:
    """read_file reads bounded UTF-8 workspace text with one-based line facts."""
    registry, workspace, _, _ = create_bindings(tmp_path)
    (workspace / "notes.md").write_text("first\nsecond\nthird\n", encoding="utf-8")

    outcome = invoke(
        registry,
        "read_file",
        {"scope": "workspace", "path": "notes.md", "start_line": 2, "max_lines": 1},
        grants=frozenset({"file.read"}),
    )

    assert outcome.ok
    assert outcome.output is not None
    assert outcome.output["content"] == "second\n"
    assert outcome.output["line_start"] == 2
    assert outcome.output["line_end"] == 2
    assert outcome.output["truncated"] is True


def test_read_file_reads_selected_skill_body_only_on_explicit_request(tmp_path: Path) -> None:
    """read_file can activate the selected Skill's SKILL.md through its virtual scope."""
    registry, _, _, _ = create_bindings(tmp_path)

    outcome = invoke(
        registry,
        "read_file",
        {"scope": "skill", "skill_code": "demo", "path": "SKILL.md"},
        grants=frozenset({"file.read"}),
    )

    assert outcome.ok
    assert outcome.output is not None
    content = outcome.output["content"]
    assert isinstance(content, str)
    assert "Use the demo skill." in content
    assert outcome.output["path"] == "SKILL.md"


def test_read_file_rejects_a_skill_that_is_not_selected_for_the_call(tmp_path: Path) -> None:
    """Skill package registration alone does not grant model access to its files."""
    registry, _, _, _ = create_bindings(tmp_path, include_skill=False)

    outcome = invoke(
        registry,
        "read_file",
        {"scope": "skill", "skill_code": "demo", "path": "SKILL.md"},
        grants=frozenset({"file.read"}),
    )

    assert not outcome.ok
    assert outcome.error_code == ToolErrorCode.FILE_NOT_FOUND


def test_read_file_rejects_paths_outside_the_workspace_scope(tmp_path: Path) -> None:
    """A model-provided parent traversal cannot escape a file Tool root."""
    registry, _, _, _ = create_bindings(tmp_path)

    outcome = invoke(
        registry,
        "read_file",
        {"scope": "workspace", "path": "../private.txt"},
        grants=frozenset({"file.read"}),
    )

    assert not outcome.ok
    assert outcome.error_code == ToolErrorCode.FILE_ACCESS_DENIED
    assert outcome.error_message is not None
    assert str(tmp_path) not in outcome.error_message


def test_read_file_reports_invalid_utf8_as_a_tool_error(tmp_path: Path) -> None:
    """Binary bytes are rejected instead of being silently decoded as text."""
    registry, workspace, _, _ = create_bindings(tmp_path)
    (workspace / "data.bin").write_bytes(b"\xff\xfe")

    outcome = invoke(
        registry,
        "read_file",
        {"scope": "workspace", "path": "data.bin"},
        grants=frozenset({"file.read"}),
    )

    assert not outcome.ok
    assert outcome.error_code == ToolErrorCode.FILE_INVALID_ENCODING


def test_read_file_keeps_the_opened_parent_when_its_path_becomes_a_symlink(tmp_path: Path) -> None:
    registry, _, scratch, _ = create_bindings(tmp_path)
    mutable = scratch / "mutable"
    mutable.mkdir()
    target = mutable / "item.txt"
    target.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "item.txt").write_text("private outside content", encoding="utf-8")
    armed = True

    def replace_parent(event: str, arguments: tuple[object, ...]) -> None:
        nonlocal armed
        if armed and event == "open" and arguments[0] in {str(target), "item.txt"}:
            armed = False
            mutable.rename(scratch / "original-mutable")
            mutable.symlink_to(outside, target_is_directory=True)

    with filesystem_audit(replace_parent):
        outcome = invoke(
            registry, "read_file", {"scope": "scratch", "path": "mutable/item.txt"}, grants=frozenset({"file.read"})
        )

    assert outcome.ok
    assert outcome.output is not None
    assert outcome.output["content"] == "inside"
    assert mutable.is_symlink()


@pytest.mark.parametrize("symlink_parent", [True, False])
def test_read_file_rejects_symlinks_to_files_outside_its_scope(tmp_path: Path, symlink_parent: bool) -> None:
    registry, workspace, _, _ = create_bindings(tmp_path)
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "item.txt").write_text("private", encoding="utf-8")
    if symlink_parent:
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
        path = "linked/item.txt"
    else:
        (workspace / "item.txt").symlink_to(outside / "item.txt")
        path = "item.txt"

    outcome = invoke(registry, "read_file", {"scope": "workspace", "path": path}, grants=frozenset({"file.read"}))

    assert not outcome.ok
    assert outcome.error_code == ToolErrorCode.FILE_ACCESS_DENIED


def test_read_file_rejects_a_fifo_without_waiting_for_a_writer(tmp_path: Path) -> None:
    registry, workspace, _, _ = create_bindings(tmp_path)
    os.mkfifo(workspace / "pipe")

    outcome = invoke(registry, "read_file", {"scope": "workspace", "path": "pipe"}, grants=frozenset({"file.read"}))

    assert not outcome.ok
    assert outcome.error_code == ToolErrorCode.FILE_ACCESS_DENIED


def test_read_file_rejects_a_file_that_grows_after_its_size_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, workspace, _, _ = create_bindings(tmp_path)
    target = workspace / "growing.txt"
    target.write_bytes(b"small")
    inode = target.stat().st_ino
    original_fstat = os.fstat
    armed = True

    def grow_after_stat(descriptor: int) -> os.stat_result:
        nonlocal armed
        metadata = original_fstat(descriptor)
        if armed and metadata.st_ino == inode:
            armed = False
            target.write_bytes(b"x" * 1_048_577)
        return metadata

    monkeypatch.setattr(os, "fstat", grow_after_stat)

    outcome = invoke(
        registry, "read_file", {"scope": "workspace", "path": "growing.txt"}, grants=frozenset({"file.read"})
    )

    assert not outcome.ok
    assert outcome.error_code == ToolErrorCode.FILE_TOO_LARGE


def test_write_file_create_does_not_overwrite_existing_workspace_text(tmp_path: Path) -> None:
    """The default create mode protects an existing workspace file."""
    registry, workspace, _, _ = create_bindings(tmp_path)
    (workspace / "result.txt").write_text("original", encoding="utf-8")

    outcome = invoke(
        registry,
        "write_file",
        {"path": "result.txt", "content": "replacement"},
        grants=frozenset({"file.write"}),
    )

    assert not outcome.ok
    assert outcome.error_code == ToolErrorCode.FILE_CONFLICT
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "original"


def test_write_file_replace_rejects_a_stale_content_digest(tmp_path: Path) -> None:
    """Conditional replace refuses to overwrite text changed since it was read."""
    registry, workspace, _, _ = create_bindings(tmp_path)
    (workspace / "result.txt").write_text("current", encoding="utf-8")

    outcome = invoke(
        registry,
        "write_file",
        {
            "path": "result.txt",
            "content": "replacement",
            "mode": "replace",
            "expected_sha256": "0" * 64,
        },
        grants=frozenset({"file.write"}),
    )

    assert not outcome.ok
    assert outcome.error_code == ToolErrorCode.FILE_CONFLICT
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "current"


@pytest.mark.parametrize("mode", ["create", "replace"])
def test_write_file_installs_text_with_the_requested_write_mode(tmp_path: Path, mode: str) -> None:
    registry, workspace, _, _ = create_bindings(tmp_path)
    target = workspace / "nested" / "result.txt"
    arguments: JsonObject = {"path": "nested/result.txt", "content": "new content", "mode": mode}
    if mode == "replace":
        target.parent.mkdir()
        target.write_text("old content", encoding="utf-8")
        arguments["expected_sha256"] = hashlib.sha256(b"old content").hexdigest()

    outcome = invoke(registry, "write_file", arguments, grants=frozenset({"file.write"}))

    assert outcome.ok
    assert target.read_text(encoding="utf-8") == "new content"
    assert outcome.output == {
        "path": "nested/result.txt",
        "bytes_written": len(b"new content"),
        "sha256": hashlib.sha256(b"new content").hexdigest(),
    }


def test_write_file_keeps_the_opened_parent_when_its_path_becomes_a_symlink(tmp_path: Path) -> None:
    registry, workspace, _, _ = create_bindings(tmp_path)
    mutable = workspace / "mutable"
    mutable.mkdir()
    target = mutable / "result.txt"
    target.write_text("original", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.txt").write_text("private", encoding="utf-8")
    armed = True

    def replace_parent_before_staging(event: str, arguments: tuple[object, ...]) -> None:
        nonlocal armed
        if not armed or event not in {"open", "tempfile.mkstemp"}:
            return
        value = arguments[0]
        if isinstance(value, str) and Path(value).name.startswith(".pleiad-write-"):
            armed = False
            mutable.rename(workspace / "original-mutable")
            mutable.symlink_to(outside, target_is_directory=True)

    with filesystem_audit(replace_parent_before_staging):
        outcome = invoke(
            registry,
            "write_file",
            {
                "path": "mutable/result.txt",
                "content": "replacement",
                "mode": "replace",
                "expected_sha256": hashlib.sha256(b"original").hexdigest(),
            },
            grants=frozenset({"file.write"}),
        )

    assert outcome.ok
    assert (outside / "result.txt").read_text(encoding="utf-8") == "private"
    assert (workspace / "original-mutable" / "result.txt").read_text(encoding="utf-8") == "replacement"


def test_write_file_rechecks_the_digest_after_staging_starts(tmp_path: Path) -> None:
    registry, workspace, _, _ = create_bindings(tmp_path)
    target = workspace / "result.txt"
    target.write_text("original", encoding="utf-8")
    armed = True

    def edit_before_staging(event: str, arguments: tuple[object, ...]) -> None:
        nonlocal armed
        if not armed or event not in {"open", "tempfile.mkstemp"}:
            return
        value = arguments[0]
        if isinstance(value, str) and Path(value).name.startswith(".pleiad-write-"):
            armed = False
            target.write_text("other writer", encoding="utf-8")

    with filesystem_audit(edit_before_staging):
        outcome = invoke(
            registry,
            "write_file",
            {
                "path": "result.txt",
                "content": "SDK replacement",
                "mode": "replace",
                "expected_sha256": hashlib.sha256(b"original").hexdigest(),
            },
            grants=frozenset({"file.write"}),
        )

    assert not outcome.ok
    assert outcome.error_code == ToolErrorCode.FILE_CONFLICT
    assert target.read_text(encoding="utf-8") == "other writer"
    assert sorted(path.name for path in workspace.iterdir()) == ["result.txt"]


def test_write_file_rechecks_the_digest_after_another_process_releases_its_lock(tmp_path: Path) -> None:
    _, workspace, scratch, _ = create_bindings(tmp_path)
    target = workspace / "result.txt"
    target.write_text("original", encoding="utf-8")
    process_context = multiprocessing.get_context("spawn")
    locks = [process_context.Event(), process_context.Event()]
    pipes = [process_context.Pipe(duplex=False), process_context.Pipe(duplex=False)]
    processes = [
        process_context.Process(target=replacement_process, args=(workspace, scratch, locking, send))
        for locking, (receive, send) in zip(locks, pipes, strict=True)
    ]
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        try:
            for process in processes:
                process.start()
            assert all(locking.wait(10.0) for locking in locks), "SDK writers did not reach the shared directory lock"
            target.write_text("other lock owner", encoding="utf-8")
        finally:
            fcntl.flock(directory, fcntl.LOCK_UN)
        for receive, send in pipes:
            send.close()
            assert receive.poll(10.0), "SDK writer did not finish after the directory lock was released"
            outcome = ToolResult.model_validate(receive.recv())
            assert not outcome.ok
            assert outcome.error_code == ToolErrorCode.FILE_CONFLICT
        for process in processes:
            process.join(timeout=10.0)
            assert process.exitcode == 0
    finally:
        os.close(directory)
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=10.0)
        for receive, send in pipes:
            receive.close()
            send.close()

    assert target.read_text(encoding="utf-8") == "other lock owner"


@pytest.mark.parametrize("sync_fails", [False, True])
def test_cancelling_a_write_waits_for_its_owned_filesystem_work_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync_fails: bool
) -> None:
    registry, workspace, _, _ = create_bindings(tmp_path)
    original_fsync = os.fsync
    release = ThreadEvent()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        staged = asyncio.Event()

        def wait_before_sync(descriptor: int) -> None:
            loop.call_soon_threadsafe(staged.set)
            if not release.wait(5.0):
                raise TimeoutError("test did not release the staged write")
            if sync_fails:
                raise OSError("synthetic filesystem failure")
            original_fsync(descriptor)

        monkeypatch.setattr(os, "fsync", wait_before_sync)
        task = asyncio.create_task(
            ToolRuntime().execute(
                registry.load("write_file"),
                ToolCall(call_id="writer", tool_code="write_file", arguments={"path": "result.txt", "content": "done"}),
                ToolContext(call_id="writer", grants=CapabilityGrants(capabilities=frozenset({"file.write"}))),
            )
        )
        try:
            await asyncio.wait_for(staged.wait(), 3.0)
            task.cancel()
            cancellation_delivered = asyncio.Event()
            loop.call_soon(cancellation_delivered.set)
            await cancellation_delivered.wait()
            assert not task.done()
            task.cancel()
            second_cancellation_delivered = asyncio.Event()
            loop.call_soon(second_cancellation_delivered.set)
            await second_cancellation_delivered.wait()
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
        if sync_fails:
            assert isinstance(caught.value.__cause__, ToolFileAccessDeniedError)
            assert isinstance(caught.value.__cause__.__cause__, OSError)

    asyncio.run(scenario())

    if sync_fails:
        assert list(workspace.iterdir()) == []
    else:
        assert (workspace / "result.txt").read_text(encoding="utf-8") == "done"
        assert sorted(path.name for path in workspace.iterdir()) == ["result.txt"]


def test_command_returns_nonzero_process_facts_and_mounts_only_selected_roots(tmp_path: Path) -> None:
    """command exposes exit facts while sending virtual paths to the Sandbox boundary."""
    registry, _, _, backend = create_bindings(tmp_path)
    backend.result = ProcessResult(
        returncode=125,
        stdout=b"out",
        stderr=b"err",
        timed_out=False,
        duration_seconds=0.25,
        output_truncated=False,
    )

    outcome = invoke(
        registry,
        "command",
        {"command": "python skills/demo/scripts/check.py"},
        grants=frozenset({"sandbox.command"}),
    )

    assert outcome.ok
    assert outcome.output is not None
    assert outcome.output["exit_code"] == 125
    assert outcome.output["stdout"] == "out"
    assert outcome.output["stderr"] == "err"
    create_command = backend.commands[1]
    assert create_command[1] == "create"
    assert "--workdir" in create_command
    assert create_command[create_command.index("--workdir") + 1] == "/pleiad"
    mounts = [create_command[index + 1] for index, value in enumerate(create_command) if value == "--mount"]
    assert mounts == [
        f"type=bind,src={(tmp_path / 'workspace').as_posix()},dst=/pleiad/workspace,readonly",
        f"type=bind,src={(tmp_path / 'scratch').as_posix()},dst=/pleiad/scratch",
        f"type=bind,src={(tmp_path / 'demo').as_posix()},dst=/pleiad/skills/demo,readonly",
    ]
    assert create_command[-4:] == ("--noprofile", "--norc", "-c", "python skills/demo/scripts/check.py")


def test_default_tool_schema_comes_from_the_parameter_model(tmp_path: Path) -> None:
    """The public schema stays aligned with the Pydantic parameter contract."""
    registry, _, _, _ = create_bindings(tmp_path)

    assert registry.load("read_file").input_schema == ReadFileArguments.model_json_schema()
