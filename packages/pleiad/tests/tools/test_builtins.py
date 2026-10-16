import asyncio
from pathlib import Path

from pleiad.contracts import CapabilityGrants, JsonObject
from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.tools.builtins.defaults import install_default_tools
from pleiad.tools.builtins.read_file import ReadFileArguments
from pleiad.tools.contracts import ToolCall, ToolContext, ToolResult
from pleiad.tools.errors import ToolErrorCode
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
    create_command = backend.commands[0]
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
