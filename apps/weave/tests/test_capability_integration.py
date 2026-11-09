import asyncio
import json
import logging
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.client.transports import FastMCPTransport, StreamableHttpTransport
from httpx import ASGITransport, AsyncClient, HTTPStatusError, Request, Response
from pleiad.contracts import CapabilityGrants
from pleiad.mcp import stdio as stdio_module
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import OpenAIStreamChunk
from pleiad.models.providers import openai
from pleiad.orchestration.graph.state import NodeStatus
from pleiad.runtime.policy import HostPolicy
from pleiad.runtime.runner import Runner
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.tools.base import ToolFailure
from pleiad.tools.contracts import ToolContext
from pleiad.tools.errors import ToolError
from pleiad.tools.runtime import ToolRuntime
from typer.testing import CliRunner

from pleiad_weave import cli
from pleiad_weave.agent.contracts import GraphSpec, NodeSpec
from pleiad_weave.agent.graph import build_graph
from pleiad_weave.api.app import create_app
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.capabilities import mcp as mcp_module
from pleiad_weave.capabilities.catalog import CapabilitySnapshot, refresh_capabilities
from pleiad_weave.capabilities.mcp import (
    McpConfiguration,
    McpConnection,
    WorkspaceOAuth,
    create_mcp_server,
    load_mcp_configuration,
)
from pleiad_weave.infrastructure.bootstrap import create_application
from pleiad_weave.infrastructure.config import Settings
from pleiad_weave.infrastructure.logging import LoggingConfig, configure_logging, shutdown_logging
from tests.provider_fake import PINNED_IMAGE, ScriptedProvider


async def catalog(service: ExecutionService) -> CapabilitySnapshot:
    return await refresh_capabilities(
        service.agents.workspace, service.agents.manifest, service.agents.sandbox, service.agents.sandbox_policy
    )


@pytest.fixture
def make_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[ScriptedProvider], ExecutionService]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "capabilities.yaml").write_text("tools: []\nskills: []\n", encoding="utf-8")
    monkeypatch.setenv("PLEIAD_MCP_CONFIG_FILE", str(tmp_path / "mcp.json"))

    def create(provider: ScriptedProvider) -> ExecutionService:
        def binding(**options: object) -> ScriptedProvider:
            return provider

        monkeypatch.setattr(openai, "OpenAIProvider", binding)
        return create_application(
            Settings(
                workspace=workspace,
                capability_file=workspace / "capabilities.yaml",
                llm_provider="openai",
                llm_protocol="openai",
                llm_model="test-model",
                llm_api_key="test-key",
                llm_vision_model=None,
                llm_speech_model=None,
                log_console=False,
                sandbox_image=PINNED_IMAGE,
            )
        )

    return create


class ScriptProcess:
    def __init__(self, stdout: bytes = b'{"count":3}', exit_code: int = 0) -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.requests: list[tuple[tuple[str, ...], bytes | None]] = []
        self.removed = False

    async def __call__(
        self, command: tuple[str, ...], *, stdin: bytes | None, output_limit: int, timeout_seconds: float | None
    ) -> ProcessResult:
        self.requests.append((command, stdin))
        if command[1] == "info":
            return ProcessResult(0, b"", b"", False, 0.0, False)
        if command[1] == "create":
            return ProcessResult(0, b"container-id\n", b"", False, 0.0, False)
        if command[1] == "start":
            return ProcessResult(0, self.stdout, b"script diagnostics", False, 0.0, False)
        if command[1] == "rm":
            self.removed = True
            return ProcessResult(0, b"container-id\n", b"", False, 0.0, False)
        if command[1] == "inspect":
            return (
                ProcessResult(1, b"", b"Error: No such object\n", False, 0.0, False)
                if self.removed
                else ProcessResult(0, str(self.exit_code).encode("ascii"), b"", False, 0.0, False)
            )
        raise AssertionError(command)


def test_workspace_tool_runs_through_sdk_oci_without_importing_user_code(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        tools = service.agents.workspace / "tools"
        tools.mkdir()
        (tools / "count.py").write_text('raise RuntimeError("Host must never import this file")\n')
        (tools / "count.json").write_text(
            json.dumps(
                {
                    "script": "count.py",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object", "required": ["count"]},
                }
            )
        )
        service.agents.manifest.write_text(
            "tools:\n  - name: count\n    code: custom.count\n    description: Count items\nskills: []\n"
        )
        process = ScriptProcess()
        service.agents.sandbox.backend.process_executor = process
        service.agents.sandbox.backend.requires_runtime_binary = False
        try:
            snapshot = await catalog(service)
            tool = snapshot.tools.load("custom.count")
            output = await tool.execute(ToolContext(call_id="call"), {"items": [1, 2, 3]})
            assert output == {"count": 3}
            assert process.removed
            start = next(request for request in process.requests if request[0][1] == "start")
            assert start[1] == b'{"items": [1, 2, 3]}'
            create = next(request[0] for request in process.requests if request[0][1] == "create")
            assert create[-1] == "/tools/count.py"
            assert create[create.index("--entrypoint") + 1] == "python"
            assert create[create.index("--network") + 1] == "none"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("stdout,exit_code", [(b"not-json", 0), (b"{}", 0), (b'{"count":3}', 1)])
def test_workspace_tool_rejects_invalid_results_and_preserves_process_failure_diagnostics(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    stdout: bytes,
    exit_code: int,
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        tools = service.agents.workspace / "tools"
        tools.mkdir()
        (tools / "count.py").write_text("print('{}')\n")
        (tools / "count.json").write_text(
            json.dumps(
                {
                    "script": "count.py",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object", "required": ["count"]},
                }
            )
        )
        service.agents.manifest.write_text(
            "tools:\n  - name: count\n    code: custom.count\n    description: Count items\nskills: []\n"
        )
        process = ScriptProcess(stdout, exit_code)
        service.agents.sandbox.backend.process_executor = process
        service.agents.sandbox.backend.requires_runtime_binary = False
        try:
            tool = (await catalog(service)).tools.load("custom.count")
            if exit_code:
                result = await tool.execute(ToolContext(call_id="call"), {})
                assert isinstance(result, ToolFailure)
                assert result.error.code == "weave.workspace_tool_failed"
                assert result.output["exit_code"] == 1 and result.output["stderr"] == "script diagnostics"
            else:
                with pytest.raises(ToolError) as failure:
                    await tool.execute(ToolContext(call_id="call"), {})
                assert failure.value.code == "weave.workspace_tool_output"
                assert failure.value.__cause__ is not None
            assert process.removed
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_log_files_redact_credentials_in_messages_and_exception_causes(tmp_path: Path) -> None:
    configure_logging(LoggingConfig(directory=tmp_path, console=False))
    try:
        error = ValueError("Authorization: Bearer private-key")
        logging.getLogger("pleiad_weave").error(
            "provider error token=private-token", exc_info=(ValueError, error, None)
        )
    finally:
        shutdown_logging()
    content = (tmp_path / "weave.log").read_text()
    assert "private-key" not in content and "private-token" not in content
    assert "[REDACTED]" in content and "ValueError" in content


@pytest.mark.parametrize("parameter", ["key", "ak", "KeY", "AK"])
def test_map_http_failures_redact_query_credentials_but_preserve_diagnostics(tmp_path: Path, parameter: str) -> None:
    request = Request("GET", f"https://maps.example/mcp?{parameter}=private-map-key&mode=driving&ak=second-key")
    response = Response(401, request=request)
    configure_logging(LoggingConfig(directory=tmp_path, console=False))
    try:
        try:
            response.raise_for_status()
        except HTTPStatusError:
            logging.getLogger("pleiad.mcp.discovery").error(
                f"MCP Tool discovery failed server=maps url={request.url}", exc_info=True
            )
    finally:
        shutdown_logging()

    content = (tmp_path / "weave.log").read_text()
    assert "private-map-key" not in content and "second-key" not in content
    assert f"https://maps.example/mcp?{parameter}=[REDACTED]&mode=driving&ak=[REDACTED]" in content
    assert "401" in content and "HTTPStatusError" in content
    assert "Traceback (most recent call last):" in content


def test_workspace_catalog_rejects_a_skill_package_symlink(
    make_service: Callable[[ScriptedProvider], ExecutionService], tmp_path: Path
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        package = tmp_path / "external"
        package.mkdir()
        (package / "SKILL.md").write_text(
            "---\nname: external\ndescription: External package\n---\nInstructions", encoding="utf-8"
        )
        skills = service.agents.workspace / "skills"
        skills.mkdir()
        (skills / "external").symlink_to(package, target_is_directory=True)
        service.agents.manifest.write_text(
            "tools: []\nskills:\n  - name: external\n    code: external\n    description: External package\n",
            encoding="utf-8",
        )
        try:
            with pytest.raises(ValueError, match="symlink"):
                await catalog(service)
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_enabled_mcp_tools_are_searchable_and_execute_through_the_sdk(
    make_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote = FastMCP("external")
    observed: list[tuple[str, dict[str, str], str | WorkspaceOAuth | None]] = []

    @remote.tool
    def echo(value: str) -> dict[str, str]:
        return {"value": value}

    @remote.tool
    def count(value: str) -> dict[str, int]:
        return {"length": len(value)}

    def transport(url: str, headers: dict[str, str], auth: str | WorkspaceOAuth | None) -> FastMCPTransport:
        observed.append((url, headers, auth))
        return FastMCPTransport(remote)

    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", transport)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TEST_MCP_TOKEN=file-token\nTEST_MCP_API_KEY=file-api-key\n", encoding="utf-8")
    monkeypatch.setenv("TEST_MCP_TOKEN", "process-token")
    monkeypatch.delenv("TEST_MCP_API_KEY", raising=False)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.agents.workspace.parent / "mcp.json").write_text(
            json.dumps(
                {
                    "servers": [
                        {
                            "code": "remote",
                            "enabled": True,
                            "url": "https://tools.example/mcp",
                            "auth": "bearer",
                            "token_env": "TEST_MCP_TOKEN",
                            "headers": {"X-Client": "weave"},
                            "header_env": {"apiKey": "TEST_MCP_API_KEY"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        try:
            snapshot = await catalog(service)
            assert {item.code for item in snapshot.tools.search("echo")} >= {"mcp.remote.echo"}
            first = await snapshot.tools.load("mcp.remote.echo").execute(ToolContext(call_id="echo"), {"value": "abc"})
            second = await snapshot.tools.load("mcp.remote.count").execute(
                ToolContext(call_id="count"), {"value": "abc"}
            )
            assert isinstance(first, dict) and first["structured_content"] == {"value": "abc"}
            assert isinstance(second, dict) and second["structured_content"] == {"length": 3}
            assert observed == [
                ("https://tools.example/mcp", {"X-Client": "weave", "apiKey": "file-api-key"}, "process-token")
            ]
            async with AsyncClient(
                transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1"
            ) as client:
                response = await client.get("/v1/resources")
                assert response.status_code == 200
                assert "mcp.remote.echo" in {item["code"] for item in response.json()["items"]}
                assert "process-token" not in response.text
                assert "file-api-key" not in response.text
                assert "tools.example" not in response.text
                detail = await client.get("/v1/resources/tools/mcp.remote.echo")
                assert detail.status_code == 200
                resource = detail.json()
                assert resource["input_schema"]["properties"]["value"]["type"] == "string"
                assert resource["output_schema"] is not None
                assert resource["configuration"]["binding"]["remote_name"] == "echo"
                assert resource["configuration"]["binding"]["connection"]["url"] == "https://tools.example/mcp"
                assert "process-token" not in detail.text and "file-api-key" not in detail.text
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("allowed_tools", [("read_record",), ()])
def test_mcp_allowlist_exposes_only_selected_remote_tools(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
    allowed_tools: tuple[str, ...],
) -> None:
    remote = FastMCP("read-only")

    @remote.tool
    def read_record() -> str:
        return "record"

    @remote.tool
    def write_record() -> str:
        raise AssertionError("An excluded MCP tool must not be executed")

    def transport(url: str, headers: dict[str, str], auth: str | WorkspaceOAuth | None) -> FastMCPTransport:
        return FastMCPTransport(remote)

    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", transport)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.agents.workspace.parent / "mcp.json").write_text(
            McpConfiguration(
                servers=(
                    McpConnection(
                        code="remote", enabled=True, url="https://tools.example/mcp", allowed_tools=allowed_tools
                    ),
                )
            ).model_dump_json(),
            encoding="utf-8",
        )
        try:
            snapshot = await catalog(service)
            assert {code for code in snapshot.tools.codes() if code.startswith("mcp.")} == {
                f"mcp.remote.{name}" for name in allowed_tools
            }
            if allowed_tools:
                result = await snapshot.tools.load("mcp.remote.read_record").execute(ToolContext(call_id="read"), {})
                assert isinstance(result, dict) and result["structured_content"] == {"result": "record"}
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_mcp_allowlist_reports_unknown_tool_names_before_exposing_capabilities(
    make_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FastMCP("empty")

    def transport(url: str, headers: dict[str, str], auth: str | WorkspaceOAuth | None) -> FastMCPTransport:
        return FastMCPTransport(remote)

    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", transport)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.agents.workspace.parent / "mcp.json").write_text(
            '{"servers":[{"code":"remote","enabled":true,"url":"https://tools.example/mcp",'
            '"allowed_tools":["missing_tool"]}]}',
            encoding="utf-8",
        )
        try:
            with pytest.raises(
                ValueError, match="MCP server remote allowed_tools names unknown remote tools: missing_tool"
            ):
                await catalog(service)
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_stdio_mcp_uses_explicit_host_environment_and_executes_tools(
    make_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote = FastMCP("stdio")
    observed: list[tuple[str, list[str], dict[str, str], bool]] = []

    @remote.tool
    def echo(value: str) -> dict[str, str]:
        return {"value": value}

    def process_transport(command: str, args: list[str], env: dict[str, str], *, keep_alive: bool) -> FastMCPTransport:
        observed.append((command, args, env, keep_alive))
        return FastMCPTransport(remote)

    monkeypatch.setattr(stdio_module, "StdioTransport", process_transport)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TEST_STDIO_TOKEN=file-token\nTEST_STDIO_REGION=local-region\n", encoding="utf-8")
    monkeypatch.setenv("TEST_STDIO_TOKEN", "process-token")
    monkeypatch.setenv("TEST_UNRELATED_SECRET", "must-not-be-forwarded")
    monkeypatch.delenv("TEST_STDIO_REGION", raising=False)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.agents.workspace.parent / "mcp.json").write_text(
            McpConfiguration(
                servers=(
                    McpConnection(
                        code="local",
                        enabled=True,
                        command="external-server",
                        args=("--stdio",),
                        env={"MODE": "stdio"},
                        env_from={"API_KEY": "TEST_STDIO_TOKEN", "REGION": "TEST_STDIO_REGION"},
                    ),
                )
            ).model_dump_json(),
            encoding="utf-8",
        )
        try:
            snapshot = await catalog(service)
            result = await snapshot.tools.load("mcp.local.echo").execute(
                ToolContext(call_id="stdio"), {"value": "ready"}
            )
            assert isinstance(result, dict) and result["structured_content"] == {"value": "ready"}
            assert (
                observed
                == [
                    (
                        "external-server",
                        ["--stdio"],
                        {"MODE": "stdio", "API_KEY": "process-token", "REGION": "local-region"},
                        False,
                    )
                ]
                * 2
            )
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_disabled_mcp_presets_do_not_require_credentials_or_connect(
    make_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise AssertionError("Disabled MCP presets must not construct a network transport")

    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", unavailable)
    monkeypatch.setattr(mcp_module, "IsolatedStdioTransport", unavailable)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.agents.workspace.parent / "mcp.json").write_text(
            '{"servers":[{"code":"disabled","url_env":"UNCONFIGURED_TEST_URL","auth":"bearer",'
            '"token_env":"UNCONFIGURED_TEST_TOKEN"},'
            '{"code":"disabled-stdio","command":"missing-server",'
            '"env_from":{"API_KEY":"UNCONFIGURED_TEST_TOKEN"}}]}',
            encoding="utf-8",
        )
        try:
            assert not any(code.startswith("mcp.") for code in (await catalog(service)).tools.codes())
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "connection",
    [
        {"url": "https://tools.example/mcp", "auth": "bearer", "token_env": "UNCONFIGURED_TEST_TOKEN"},
        {"url": "https://tools.example/mcp", "header_env": {"apiKey": "UNCONFIGURED_TEST_TOKEN"}},
        {"command": "missing-server", "env_from": {"API_KEY": "UNCONFIGURED_TEST_TOKEN"}},
    ],
)
def test_enabled_mcp_connection_reports_missing_credentials_before_discovery(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    connection: dict[str, object],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("UNCONFIGURED_TEST_TOKEN", raising=False)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.agents.workspace.parent / "mcp.json").write_text(
            json.dumps({"servers": [{"code": "remote", "enabled": True, **connection}]}),
            encoding="utf-8",
        )
        try:
            with pytest.raises(ValueError, match="UNCONFIGURED_TEST_TOKEN"):
                await catalog(service)
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "servers",
    [
        [{"code": "duplicate", "url": "https://example.com/mcp"}] * 2,
        [{"code": "remote", "url": "file:///tmp/mcp"}],
        [{"code": "remote", "url": "https://example.com", "url_env": "OTHER_URL"}],
        [{"code": "remote", "url": "https://example.com", "auth": "bearer"}],
        [{"code": "remote", "url": "https://example.com", "headers": {"Authorization": "Bearer secret"}}],
        [{"code": "remote", "url": "https://example.com", "header_env": {"aUthorization": "TOKEN"}}],
        [
            {
                "code": "remote",
                "url": "https://example.com",
                "headers": {"X-Key": "public"},
                "header_env": {"x-key": "KEY"},
            }
        ],
        [{"code": "remote", "url": "https://example.com", "headers": {"X-Key": "public", "x-key": "duplicate"}}],
        [{"code": "remote", "url": "https://example.com", "command": "uvx"}],
        [{"code": "remote", "command": " "}],
        [{"code": "remote", "command": "uvx", "auth": "oauth"}],
        [{"code": "remote", "command": "uvx", "headers": {"X-Client": "weave"}}],
        [{"code": "remote", "command": "uvx", "header_env": {"apiKey": "KEY"}}],
        [{"code": "remote", "url": "https://example.com", "args": ["server"]}],
        [{"code": "remote", "url": "https://example.com", "env_from": {"KEY": "TOKEN"}}],
        [{"code": "remote", "command": "uvx", "env": {"KEY": "public"}, "env_from": {"KEY": "TOKEN"}}],
        [{"code": "remote", "command": "uvx", "env_from": {"KEY": "bad name"}}],
        [{"code": "remote", "url": "https://example.com", "allowed_tools": ["read", "read"]}],
        [{"code": "remote", "url": "https://example.com", "allowed_tools": [" "]}],
    ],
)
def test_invalid_mcp_configuration_is_rejected(servers: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError):
        McpConfiguration.model_validate({"servers": servers})


def test_mcp_oauth_requires_terminal_login_instead_of_opening_a_browser(tmp_path: Path) -> None:
    auth = WorkspaceOAuth("https://example.com/mcp", tmp_path / "tokens", interactive=False)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="pleiad mcp-login"):
            await auth.redirect_handler("https://example.com/consent")

    asyncio.run(scenario())


def test_mcp_bearer_credentials_can_be_loaded_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_MCP_TOKEN", raising=False)
    (tmp_path / ".env").write_text("TEST_MCP_TOKEN=file-token\n", encoding="utf-8")
    variable_name = "TEST_MCP_TOKEN"
    server = create_mcp_server(
        McpConnection(code="remote", url="https://example.com/mcp", auth="bearer", token_env=variable_name),
        tmp_path,
    )
    assert isinstance(server.client.transport, StreamableHttpTransport)
    assert server.client.transport.url == "https://example.com/mcp"
    assert server.client.transport.auth is not None


def test_mcp_oauth_state_cannot_be_stored_in_the_tool_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLEIAD_MCP_AUTH_DIRECTORY", str(tmp_path / "credentials"))
    with pytest.raises(ValueError, match="outside the tool-visible workspace"):
        create_mcp_server(McpConnection(code="remote", url="https://example.com/mcp", auth="oauth"), tmp_path)


def test_mcp_configuration_cannot_be_read_from_the_tool_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLEIAD_MCP_CONFIG_FILE", str(tmp_path / "mcp.json"))
    (tmp_path / "mcp.json").write_text('{"servers":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="outside the tool-visible workspace"):
        load_mcp_configuration(tmp_path)


def test_mcp_login_checks_a_disabled_preset_without_starting_models_or_running_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FastMCP("login")
    actions: list[str] = []

    @remote.tool
    def write_record(value: str) -> str:
        actions.append(value)
        return value

    def transport(url: str, headers: dict[str, str], auth: str | WorkspaceOAuth | None) -> FastMCPTransport:
        return FastMCPTransport(remote)

    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", transport)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLEIAD_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("PLEIAD_MCP_CONFIG_FILE", str(tmp_path / "mcp.json"))
    (tmp_path / "mcp.json").write_text(
        '{"servers":[{"code":"remote","url":"https://example.com/mcp"}]}', encoding="utf-8"
    )
    result = CliRunner().invoke(cli.app, ["mcp-login", "remote"])
    assert result.exit_code == 0, result.output
    assert "MCP connection ready: remote (1 tools)" in result.output
    assert actions == []


@pytest.mark.parametrize(
    ("skill_code", "instruction"),
    [
        ("systematic-debugging", "Do not propose a fix before collecting evidence of the root cause."),
        ("verification-before-completion", "Evidence must precede a completion claim."),
        ("travel-planning", "查询时有库存也不等于已预订。"),
        ("web-research", "搜索摘要仅是线索。"),
        ("document-writing", "不能以截断内容重建整篇文档。"),
    ],
)
def test_graph_node_reads_selected_skill_body_only_after_its_tool_request(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    skill_code: str,
    instruction: str,
) -> None:
    source = Path(__file__).resolve().parents[3] / "workspace"
    expected_content = (source / "skills" / skill_code / "SKILL.md").read_text(encoding="utf-8")

    class SkillReadingProvider(ScriptedProvider):
        async def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
            if len(self.requests) == 1:
                observed = request.messages[-1]
                assert observed.role == "tool" and isinstance(observed.content, str)
                envelope = json.loads(observed.content)
                assert envelope["ok"] is True
                output = envelope["output"]
                assert output["scope"] == "skill" and output["path"] == "SKILL.md"
                assert output["content"] == expected_content
                self.responses = iter((json.dumps({"skill_content": output["content"]}),))
            async for chunk in super().stream(request):
                yield chunk

    async def scenario() -> None:
        provider = SkillReadingProvider(
            ({"name": "read_file", "arguments": {"scope": "skill", "skill_code": skill_code, "path": "SKILL.md"}},)
        )
        service = make_service(provider)
        shutil.copytree(source / "skills", service.agents.workspace / "skills")
        shutil.copytree(source / "tools", service.agents.workspace / "tools")
        shutil.copyfile(source / "capabilities.yaml", service.agents.manifest)
        try:
            snapshot = await catalog(service)
            runner = Runner(
                tools=snapshot.tools,
                skills=snapshot.skills,
                tool_runtime=ToolRuntime(),
                host_policy=HostPolicy(grants=CapabilityGrants(capabilities=frozenset({"file.read"}))),
            )
            graph = build_graph(
                GraphSpec(
                    graph_id="read-selected-skill",
                    nodes=(
                        NodeSpec(
                            node_id="review",
                            objective="Read the selected skill body",
                            skill_codes=(skill_code,),
                            tool_codes=("read_file",),
                        ),
                    ),
                ),
                service.agents.models,
                runner,
                session_store=service.sessions,
            )
            result = await graph.nodes[0].run({})
            assert result.status is NodeStatus.SUCCEEDED
            assert result.output == {"skill_content": expected_content}
            first_input = "\n".join(
                message.content for message in provider.requests[0].messages if isinstance(message.content, str)
            )
            assert f"# Selected skills\n\n- `{skill_code}`:" in first_input
            assert instruction not in first_input
            assert instruction in expected_content
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_refreshing_capabilities_discovers_a_new_skill_without_restarting_the_service(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        try:
            before = await catalog(service)
            package = service.agents.workspace / "skills" / "new-skill"
            package.mkdir(parents=True)
            (package / "SKILL.md").write_text(
                "---\nname: new-skill\ndescription: Newly installed skill\n---\nRead these instructions."
            )
            service.agents.manifest.write_text(
                "tools: []\nskills:\n  - name: new-skill\n    code: new-skill\n    description: Newly installed skill\n"
            )
            after = await catalog(service)
            assert not before.skills.contains("new-skill")
            assert after.skills.load("new-skill").instructions == "Read these instructions."
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_resource_model_details_preserve_configuration_and_mask_nested_credentials(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(())
        service = make_service(provider)
        service.agents.models = {
            "text": OpenAIModelClient(
                "openai",
                "configured-model",
                "secret-model-key",
                base_url="https://private-user:private-password@models.example/v1?api_key=query-secret&region=cn",
                timeout_seconds=33,
                max_retries=4,
                headers={"Authorization": "Bearer secret-header", "X-Client": "weave", "Cookie": "private-cookie"},
                provider_options={
                    "max_tokens": 1200,
                    "nested": [{"password": "nested-secret", "alias": "nested-secret", "region": "cn"}],
                    "environment": {
                        "SERVICE_API_KEY": "private-env-key",
                        "AWS_ACCESS_KEY_ID": "private-aws-key-id",
                        "ACCESS_TOKEN": "private-access-token",
                        "PRIVATE_KEY": "private-signing-key",
                        "LANG": "zh_CN",
                    },
                    "credential_alias": "secret-model-key",
                },
            )
        }
        try:
            async with AsyncClient(
                transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1"
            ) as client:
                response = await client.get("/v1/resources/models/text")
                assert response.status_code == 200
                data = response.json()
                configuration = data["configuration"]
                assert configuration["model"] == "configured-model"
                assert configuration["provider"] == "openai" and configuration["protocol"] == "openai"
                assert configuration["timeout_seconds"] == 33 and configuration["max_retries"] == 4
                assert configuration["provider_options"]["max_tokens"] == 1200
                assert configuration["provider_options"]["environment"]["LANG"] == "zh_CN"
                assert configuration["headers"]["X-Client"] == "weave"
                assert "models.example/v1" in configuration["base_url"] and "region=cn" in configuration["base_url"]
                assert configuration["api_key"] == "[REDACTED]"
                for secret in (
                    "secret-model-key",
                    "private-user",
                    "private-password",
                    "query-secret",
                    "secret-header",
                    "private-cookie",
                    "nested-secret",
                    "private-env-key",
                    "private-aws-key-id",
                    "private-access-token",
                    "private-signing-key",
                ):
                    assert secret not in response.text
                assert provider.requests == []
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_resource_tool_details_include_schemas_permissions_and_script_binding_without_execution(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        tools = service.agents.workspace / "tools"
        tools.mkdir()
        (tools / "inspect.py").write_text('raise RuntimeError("Resource viewing must never run scripts")\n')
        (tools / "inspect.json").write_text(
            json.dumps(
                {
                    "script": "inspect.py",
                    "input_schema": {
                        "type": "object",
                        "required": ["api_key"],
                        "description": "Configured credential test-key must never appear here",
                        "properties": {
                            "api_key": {"type": "string", "default": "private-schema-default"},
                            "region": {"type": "string", "examples": ["test-key", "cn"]},
                        },
                    },
                    "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                }
            )
        )
        service.agents.manifest.write_text(
            "tools:\n  - name: inspect\n    code: custom.inspect\n    description: Inspect data test-key\nskills: []\n"
        )
        service.agents.approval_tool_codes = frozenset({"custom.inspect"})
        try:
            async with AsyncClient(
                transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1"
            ) as client:
                response = await client.get("/v1/resources/tools/custom.inspect")
                assert response.status_code == 200
                detail = response.json()
                assert detail["metadata"] == {
                    "name": "inspect",
                    "code": "custom.inspect",
                    "description": "Inspect data [REDACTED]",
                }
                assert detail["input_schema"]["properties"]["api_key"] == {
                    "type": "string",
                    "default": "[REDACTED]",
                }
                assert detail["input_schema"]["required"] == ["api_key"]
                assert detail["input_schema"]["properties"]["region"]["examples"] == ["[REDACTED]", "cn"]
                assert "test-key" not in response.text
                assert detail["output_schema"]["properties"]["ok"]["type"] == "boolean"
                assert detail["configuration"]["required_permissions"] == ["sandbox.command"]
                assert detail["configuration"]["approval_required"] is True
                assert detail["configuration"]["binding"]["script"] == "inspect.py"
                assert detail["configuration"]["binding"]["sandbox_policy"]["image"] == PINNED_IMAGE
                assert "private-schema-default" not in response.text
                listing = await client.get("/v1/resources?kind=tools")
                assert "test-key" not in listing.text
                assert all(
                    "input_schema" not in item and "configuration" not in item for item in listing.json()["items"]
                )
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_resource_skill_details_include_full_document_metadata_and_package_file_inventory(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        package = service.agents.workspace / "skills" / "research"
        (package / "references").mkdir(parents=True)
        (package / "scripts").mkdir()
        document = (
            "---\nname: research\ndescription: Research evidence\nlicense: MIT\ncompatibility: Python 3.12\n"
            "metadata:\n  author: Example\nallowed-tools: read_file command\n---\n"
            "# Research\n\nRead all evidence.\n\nAuthorization: Bearer private-skill-key\n\nFinish with a report.\n"
        )
        (package / "SKILL.md").write_text(document)
        (package / "references" / "guide.md").write_text("Evidence guide")
        (package / "scripts" / "check.py").write_text('raise RuntimeError("Must not execute")')
        service.agents.manifest.write_text(
            "tools: []\nskills:\n  - name: research\n    code: research\n    description: Research evidence\n"
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1"
            ) as client:
                response = await client.get("/v1/resources/skills/research")
                assert response.status_code == 200
                detail = response.json()
                assert detail["content"] == document.replace("Bearer private-skill-key", "[REDACTED]")
                assert detail["metadata"]["package"]["license"] == "MIT"
                assert detail["metadata"]["package"]["compatibility"] == "Python 3.12"
                assert detail["metadata"]["package"]["metadata"] == {"author": "Example"}
                assert detail["metadata"]["package"]["allowed_tools"] == ["read_file", "command"]
                assert detail["files"] == ["SKILL.md", "references/guide.md", "scripts/check.py"]
                assert detail["configuration"]["references"] == ["references/guide.md"]
                assert "private-skill-key" not in response.text
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_resource_environment_details_show_actual_workspace_and_sandbox_limits(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        try:
            async with AsyncClient(
                transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1"
            ) as client:
                response = await client.get("/v1/resources/environments/workspace")
                assert response.status_code == 200
                detail = response.json()
                assert detail["configuration"]["workspace"] == str(service.agents.workspace)
                assert detail["configuration"]["backend"] == service.agents.sandbox.backend.runtime
                assert detail["configuration"]["sandbox_policy"] == service.agents.sandbox_policy.model_dump(
                    mode="json"
                )
                assert (await client.get("/v1/resources/environments/missing")).status_code == 404
        finally:
            await service.shutdown()

    asyncio.run(scenario())
