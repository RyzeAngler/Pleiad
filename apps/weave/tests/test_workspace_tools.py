import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pleiad.contracts import CapabilityGrants, JsonObject
from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.tools.contracts import ToolCall, ToolContext, ToolResult
from pleiad.tools.runtime import ToolRuntime

from pleiad_weave.capabilities.catalog import load_capabilities
from pleiad_weave.capabilities.tools import JSON_OBJECT_ADAPTER, ToolBinding
from pleiad_weave.infrastructure.sandbox.provisioning import default_policy

CSV_TOOL = Path(__file__).resolve().parents[3] / "workspace" / "tools" / "csv_profile.json"


def run_csv_profile(workspace: Path, arguments: JsonObject) -> subprocess.CompletedProcess[str]:
    binding = ToolBinding.model_validate_json(CSV_TOOL.read_bytes())
    Draft202012Validator(binding.input_schema).validate(arguments)
    result = subprocess.run(  # noqa: S603 - Run the fixed repository script with the current test interpreter.
        (sys.executable, str(CSV_TOOL.parent / binding.script)),
        input=json.dumps(arguments),
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode == 0:
        output = JSON_OBJECT_ADAPTER.validate_json(result.stdout)
        Draft202012Validator(binding.output_schema).validate(output)
    return result


def test_csv_profile_preserves_quoted_text_and_counts_empty_cells(tmp_path: Path) -> None:
    (tmp_path / "orders.csv").write_text(
        '\ufeffname,amount,code\n"one, ""quoted""",1.5,001\n\n"two\nlines",,002\n,not-a-number,003\n spaced ,2.5,\n',
        encoding="utf-8",
    )

    result = run_csv_profile(tmp_path, {"path": "orders.csv", "numeric_columns": ["amount"], "sample_size": 2})

    assert result.returncode == 0, result.stderr
    assert JSON_OBJECT_ADAPTER.validate_json(result.stdout) == {
        "path": "orders.csv",
        "row_count": 4,
        "columns": [
            {"name": "name", "empty_count": 1, "samples": ['one, "quoted"', "two\nlines"], "numeric": None},
            {
                "name": "amount",
                "empty_count": 1,
                "samples": ["1.5", "not-a-number"],
                "numeric": {"valid_count": 2, "invalid_count": 1, "minimum": 1.5, "maximum": 2.5, "mean": 2.0},
            },
            {"name": "code", "empty_count": 1, "samples": ["001", "002"], "numeric": None},
        ],
    }


def test_csv_profile_does_not_infer_types_or_normalize_text_and_limits_samples(tmp_path: Path) -> None:
    (tmp_path / "values.csv").write_text(f"code\n001\n \n{'x' * 250}\nNULL\n", encoding="utf-8")

    result = run_csv_profile(tmp_path, {"path": "values.csv"})

    assert result.returncode == 0, result.stderr
    assert JSON_OBJECT_ADAPTER.validate_json(result.stdout) == {
        "path": "values.csv",
        "row_count": 4,
        "columns": [{"name": "code", "empty_count": 0, "samples": ["001", " ", "x" * 200], "numeric": None}],
    }


def test_csv_profile_excludes_nonfinite_numbers_without_overflowing_the_mean(tmp_path: Path) -> None:
    (tmp_path / "values.csv").write_text(
        'amount\nNaN\nsNaN\nInfinity\n-infinity\n1e9999\nbad\n""\n1e308\n1e308\n', encoding="utf-8"
    )

    result = run_csv_profile(tmp_path, {"path": "values.csv", "numeric_columns": ["amount"], "sample_size": 0})

    assert result.returncode == 0, result.stderr
    assert JSON_OBJECT_ADAPTER.validate_json(result.stdout) == {
        "path": "values.csv",
        "row_count": 9,
        "columns": [
            {
                "name": "amount",
                "empty_count": 1,
                "samples": [],
                "numeric": {"valid_count": 2, "invalid_count": 6, "minimum": 1e308, "maximum": 1e308, "mean": 1e308},
            }
        ],
    }


def test_csv_profile_header_only_has_no_numeric_statistics(tmp_path: Path) -> None:
    (tmp_path / "values.csv").write_text("amount\n", encoding="utf-8")

    result = run_csv_profile(tmp_path, {"path": "values.csv", "numeric_columns": ["amount"]})

    assert result.returncode == 0, result.stderr
    assert JSON_OBJECT_ADAPTER.validate_json(result.stdout) == {
        "path": "values.csv",
        "row_count": 0,
        "columns": [
            {
                "name": "amount",
                "empty_count": 0,
                "samples": [],
                "numeric": {"valid_count": 0, "invalid_count": 0, "minimum": None, "maximum": None, "mean": None},
            }
        ],
    }


@pytest.mark.parametrize("escape", ["parent", "absolute", "symlink"])
def test_csv_profile_rejects_paths_outside_the_workspace(tmp_path: Path, escape: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.csv"
    outside.write_text("secret\nshould-not-be-returned\n", encoding="utf-8")
    path = "../private.csv"
    if escape == "absolute":
        path = str(outside)
    elif escape == "symlink":
        (workspace / "linked.csv").symlink_to(outside)
        path = "linked.csv"

    result = run_csv_profile(workspace, {"path": path})

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Path escapes the configured workspace" in result.stderr
    assert "should-not-be-returned" not in result.stderr


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("", "CSV must have a non-empty, unique header"),
        ("name,name\na,b\n", "CSV must have a non-empty, unique header"),
        ("name,\na,b\n", "CSV must have a non-empty, unique header"),
        ("name,amount\na\n", "does not match the header column count"),
        ("name,amount\na,1,extra\n", "does not match the header column count"),
        ('name\n"unclosed\n', "unexpected end of data"),
    ],
)
def test_csv_profile_rejects_malformed_records_instead_of_dropping_values(
    tmp_path: Path, content: str, error: str
) -> None:
    (tmp_path / "broken.csv").write_text(content, encoding="utf-8")

    result = run_csv_profile(tmp_path, {"path": "broken.csv"})

    assert result.returncode != 0
    assert result.stdout == ""
    assert error in result.stderr


def test_csv_profile_rejects_an_unknown_numeric_column(tmp_path: Path) -> None:
    (tmp_path / "values.csv").write_text("amount\n1\n", encoding="utf-8")

    result = run_csv_profile(tmp_path, {"path": "values.csv", "numeric_columns": ["missing"]})

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Unknown numeric columns: missing" in result.stderr


def test_capability_bindings_hide_product_state_from_file_and_workspace_script_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scripts = workspace / "tools"
    scripts.mkdir(parents=True)
    private = workspace / "runtime-v2"
    private.mkdir()
    (private / "product.sqlite3").write_bytes(b"host-owned product state")
    (scripts / "check.py").write_text('raise RuntimeError("Host must not execute workspace code")\n', encoding="utf-8")
    (scripts / "check.json").write_text(
        json.dumps(
            {
                "script": "check.py",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object", "required": ["ok"]},
            }
        ),
        encoding="utf-8",
    )
    manifest = workspace / "capabilities.yaml"
    manifest.write_text(
        "tools:\n  - name: check\n    code: custom.check\n    description: Check a file\nskills: []\n", encoding="utf-8"
    )
    commands: list[tuple[str, ...]] = []
    removed = False

    async def process(
        command: tuple[str, ...], *, stdin: bytes | None, output_limit: int, timeout_seconds: float | None
    ) -> ProcessResult:
        nonlocal removed
        commands.append(command)
        if command[1] == "create":
            return ProcessResult(0, b"container-id\n", b"", False, 0.0, False)
        if command[1] == "start":
            return ProcessResult(0, b'{"ok":true}', b"", False, 0.0, False)
        if command[1] == "rm":
            removed = True
        if command[1] == "inspect":
            return (
                ProcessResult(1, b"", b"Error: No such object\n", False, 0.0, False)
                if removed
                else ProcessResult(0, b"0\n", b"", False, 0.0, False)
            )
        return ProcessResult(0, b"", b"", False, 0.0, False)

    snapshot = load_capabilities(
        workspace,
        manifest,
        Sandbox(OciSandbox(process_executor=process)),
        SandboxPolicy(image="python@sha256:" + "a" * 64, allow_shell_command=True),
    )

    async def scenario() -> None:
        context = ToolContext(call_id="read", grants=CapabilityGrants(capabilities=frozenset({"file.read"})))
        denied = await ToolRuntime().execute(
            snapshot.tools.load("read_file"),
            ToolCall(
                call_id="read",
                tool_code="read_file",
                arguments={"scope": "workspace", "path": "runtime-v2/product.sqlite3"},
            ),
            context,
        )
        assert isinstance(denied, ToolResult) and not denied.ok
        assert denied.error_code == "tool.file.access_denied"
        output = await snapshot.tools.load("custom.check").execute(ToolContext(call_id="script"), {})
        assert output == {"ok": True}

    asyncio.run(scenario())
    create = next(command for command in commands if command[1] == "create")
    mounts = [create[index + 1] for index, value in enumerate(create) if value == "--mount"]
    assert len(mounts) == 3
    assert mounts[0] == f"type=bind,src={workspace.as_posix()},dst=/workspace"
    assert mounts[1].endswith(",dst=/workspace/runtime-v2,readonly")
    assert ".pleiad-masks/" in mounts[1]
    assert mounts[2] == f"type=bind,src={scripts.as_posix()},dst=/tools,readonly"
    assert (private / "product.sqlite3").read_bytes() == b"host-owned product state"


@pytest.mark.oci
def test_real_workspace_tool_reads_sdk_written_files_and_keeps_host_scripts_read_only(tmp_path: Path) -> None:
    image = os.environ.get("PLEIAD_DOCKER_SANDBOX_IMAGE")
    if image is None:
        pytest.skip("set PLEIAD_DOCKER_SANDBOX_IMAGE to a pre-pulled sha256 image digest")
    workspace = tmp_path / "workspace"
    scripts = workspace / "tools"
    scripts.mkdir(parents=True)
    script = scripts / "process.py"
    script.write_text(
        "import errno\n"
        "import json\n"
        "from pathlib import Path\n"
        "source = Path('brief.txt').read_text()\n"
        "Path('result.txt').write_text(source.upper())\n"
        "try:\n"
        "    Path('/tools/changed.py').write_text('forbidden')\n"
        "except OSError as error:\n"
        "    assert error.errno == errno.EROFS\n"
        "else:\n"
        "    raise RuntimeError('Host scripts were writable')\n"
        "print(json.dumps({'source': source, 'result': Path('result.txt').read_text()}))\n",
        encoding="utf-8",
    )
    (scripts / "process.json").write_text(
        json.dumps(
            {
                "script": "process.py",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object", "required": ["source", "result"]},
            }
        ),
        encoding="utf-8",
    )
    manifest = workspace / "capabilities.yaml"
    manifest.write_text(
        "tools:\n  - name: process\n    code: custom.process\n    description: Process task data\nskills: []\n"
    )
    work_root = workspace / "work" / "space" / "execution"
    snapshot = load_capabilities(workspace, manifest, Sandbox(OciSandbox()), default_policy(image), work_root=work_root)

    async def scenario() -> None:
        grants = CapabilityGrants(capabilities=frozenset({"file.read", "file.write", "sandbox.command"}))
        await snapshot.tools.load("write_file").execute(
            ToolContext(call_id="write", grants=grants), {"path": "brief.txt", "content": "Task bytes"}
        )
        output = await ToolRuntime().execute(
            snapshot.tools.load("custom.process"),
            ToolCall(call_id="process", tool_code="custom.process"),
            ToolContext(call_id="process", grants=grants),
        )
        assert isinstance(output, ToolResult) and output.ok
        assert output.output == {"source": "Task bytes", "result": "TASK BYTES"}

    asyncio.run(scenario())
    assert (work_root / "result.txt").read_bytes() == b"TASK BYTES"
    assert not (scripts / "changed.py").exists()
