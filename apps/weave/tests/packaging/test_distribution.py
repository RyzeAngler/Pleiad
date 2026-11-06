import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

pytestmark = pytest.mark.packaging


@pytest.mark.parametrize("distribution_format", ["wheel", "sdist"])
def test_distributions_install_without_the_checkout_and_ship_only_product_resources(
    tmp_path: Path, distribution_format: str
) -> None:
    repository = Path(__file__).resolve().parents[4]
    output = tmp_path / "dist"
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Packaging verification requires the project's uv tool")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    for project in ("packages/pleiad", "apps/weave"):
        subprocess.run(  # noqa: S603 - Fixed uv/temporary interpreter commands; no user-supplied argv.
            (uv, "build", "--project", str(repository / project), "--out-dir", str(output)),
            cwd=tmp_path,
            env=environment,
            check=True,
            timeout=120,
            capture_output=True,
            text=True,
        )
    sdk = next(output.glob("pleiad-*.whl"))
    app = next(output.glob("pleiad_weave-*.whl"))
    install_sdk = sdk if distribution_format == "wheel" else next(output.glob("pleiad-*.tar.gz"))
    install_app = app if distribution_format == "wheel" else next(output.glob("pleiad_weave-*.tar.gz"))
    with zipfile.ZipFile(sdk) as archive:
        names = archive.namelist()
        assert "pleiad/runtime/execution/read_tool_result.py" in names
        assert "pleiad/messages/results.py" in names
        assert "pleiad/orchestration/budget.py" in names
        assert "pleiad/tools/builtins/read_tool_result.py" not in names
    with zipfile.ZipFile(app) as archive:
        names = archive.namelist()
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_path).decode("utf-8"))
        sdk_requirements = [
            requirement
            for value in metadata.get_all("Requires-Dist", [])
            if (requirement := Requirement(value)).name == "pleiad"
        ]
        assert sdk_requirements
        for requirement in sdk_requirements:
            assert Version("0.1.2") in requirement.specifier
            assert Version("0.1.1") not in requirement.specifier
        assert "pleiad_weave/agent/graph.py" in names
        assert "pleiad_weave/capabilities/approval.py" in names
        assert "pleiad_weave/memory_cli.py" in names
        assert "pleiad_weave/evaluation.py" in names
        assert "pleiad_weave/tui/screens/approvals.py" in names
        assert "pleiad_weave/tui/styles/theme.tcss" in names
        assert "pleiad_weave/web/assets/index.html" in names
        assert any(name.startswith("pleiad_weave/web/assets/assets/") and name.endswith(".js") for name in names)
        assert any(name.startswith("pleiad_weave/web/assets/assets/") and name.endswith(".css") for name in names)
        assert "pleiad_weave/resources/sandbox/images/python/Dockerfile" in names
        assert "pleiad_weave/application/supervisor.py" not in names
        assert "pleiad_weave/infrastructure/storage/traces.py" not in names
        assert not any(name.startswith(("workspace/", "pleiad/", "src/")) for name in names)
    with tarfile.open(next(output.glob("pleiad_weave-*.tar.gz"))) as archive:
        names = archive.getnames()
        assert any(name.endswith("/src/agent/graph.py") for name in names)
        assert not any("/workspace/" in name for name in names)
    installed = tmp_path / "installed"
    subprocess.run(  # noqa: S603 - Fixed uv/temporary interpreter commands; no user-supplied argv.
        (uv, "venv", "--python", sys.executable, str(installed)),
        check=True,
        timeout=120,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    python = installed / "bin" / "python"
    subprocess.run(  # noqa: S603 - Fixed uv/temporary interpreter commands; no user-supplied argv.
        (uv, "pip", "install", "--python", str(python), str(install_sdk), str(install_app)),
        check=True,
        timeout=120,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    script = """
import importlib.util
import asyncio
import re
from importlib.resources import files
from pathlib import Path
from pleiad.agent.agent import Agent
from pleiad.schemas import validate_json_schema
from pleiad.memory.short_memory import ShortMemory
from pleiad.messages.message import AssistantMessage, ToolCall, ToolMessage, UserMessage
from pleiad.runtime.execution.read_tool_result import ReadToolResultTool
from pleiad.tools.contracts import ToolCall as ExecutionToolCall, ToolContext, ToolResult
from pleiad.tools.runtime import ToolRuntime
from pleiad_weave.evaluation import EvaluationCase, score_output
from pleiad_weave.agent.graph import build_graph
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.bootstrap import create_application
from pleiad_weave.infrastructure.sandbox.images import bundled_python_image_definition
validate_json_schema({"type": "object"})
assert "site-packages" in str(files("pleiad_weave"))
assert importlib.util.find_spec("pleiad_weave.application.supervisor") is None
assert importlib.util.find_spec("pleiad_weave.infrastructure.storage.traces") is None
with bundled_python_image_definition() as (dockerfile, lockfile):
    assert "USER 65532:65532" in dockerfile.read_text()
    assert "standard library by default" in lockfile.read_text()
assert files("pleiad_weave.tui").joinpath("styles/theme.tcss").read_text()
web = files("pleiad_weave.web").joinpath("assets")
index = web.joinpath("index.html").read_text()
assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', index)
assert assets
font_faces = []
for asset in assets:
    resource = web.joinpath(asset.removeprefix("/"))
    assert resource.read_bytes()
    if asset.endswith(".css"):
        font_faces.extend(re.findall(r"@font-face[ ]*[{]([^}]+)[}]", resource.read_text()))
assert font_faces and any("MiSans" in face for face in font_faces)
font_assets = set()
for face in font_faces:
    sources = re.findall(r"url[(]([^)]*)[)]", face)
    assert sources
    for source in sources:
        asset = source.strip("\\\"'")
        assert asset.startswith("/assets/") and asset.endswith(".woff2"), asset
        font_assets.add(asset)
for asset in font_assets:
    assert web.joinpath(asset.removeprefix("/")).read_bytes().startswith(b"wOF2")
license_assets = []
for link in re.findall(r"<link[^>]+>", index):
    if 'rel="license"' in link:
        target = re.search(r'href="([^\"]+)"', link)
        assert target is not None
        license_assets.append(target[1])
assert len(license_assets) == 2
font_licenses = [asset for asset in license_assets if asset.endswith(".pdf")]
font_notices = [asset for asset in license_assets if asset.endswith(".txt")]
assert len(font_licenses) == len(font_notices) == 1
assert web.joinpath(font_licenses[0].removeprefix("/")).read_bytes().startswith(b"%PDF-")
notice = web.joinpath(font_notices[0].removeprefix("/")).read_text()
assert "MiSans" in notice and "Xiaomi" in notice
case = EvaluationCase(case_id="smoke", objective="task", files={"input": "value"}, expected={"n": 1})
assert score_output(case, '{"n":1}')
async def read_result() -> None:
    memory = ShortMemory.in_memory("smoke")
    turn = await memory.begin("agent", (UserMessage(content="task"),))
    await memory.complete(turn.turn_id, (
        AssistantMessage(tool_calls=(ToolCall(id="lookup", name="lookup", arguments="{}"),)),
        ToolMessage(message_id="result", tool_call_id="lookup", content="original evidence"),
    ))
    result = await ToolRuntime().execute(
        ReadToolResultTool(memory),
        ExecutionToolCall(call_id="read", tool_code="read_tool_result", arguments={"message_id": "result"}),
        ToolContext(call_id="read"),
    )
    assert isinstance(result, ToolResult) and result.ok and result.output is not None
    assert result.output["content"] == "original evidence"
    await memory.clear()
    assert await memory.read() == ()
    assert await memory.active() is None
    restarted = await memory.begin("agent", (UserMessage(content="new task"),))
    assert restarted.sequence == 1
asyncio.run(read_result())
print("clean install verified")
"""
    result = subprocess.run(  # noqa: S603 - Fixed uv/temporary interpreter commands; no user-supplied argv.
        (str(python), "-c", script),
        check=True,
        timeout=120,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "clean install verified"
    cli_result = subprocess.run(  # noqa: S603 - Fixed uv/temporary interpreter commands; no user-supplied argv.
        (str(installed / "bin" / "pleiad"), "--help"),
        cwd=tmp_path,
        env=environment,
        check=True,
        timeout=120,
        capture_output=True,
        text=True,
    )
    assert "run" in cli_result.stdout and "serve" in cli_result.stdout and "tui" in cli_result.stdout
    assert "approval" in cli_result.stdout and "memory" in cli_result.stdout
