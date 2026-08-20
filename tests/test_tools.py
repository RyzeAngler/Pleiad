"""Tool manifest, Zoo, and runtime boundary tests."""

import asyncio
from pathlib import Path

from pleiad.tools.base import FunctionTool
from pleiad.tools.builtins import register_builtins
from pleiad.tools.contracts import ToolCall, ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.manifest import load_tool_descriptors
from pleiad.tools.runtime import ToolRuntime
from pleiad.tools.workspace import register_workspace_tools
from pleiad.tools.zoo import ToolZoo


def test_manifest_has_only_the_three_public_tool_fields(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.yaml"
    path.write_text(
        "tools:\n  - name: echo\n    code: test.echo\n    description: 回显\n", encoding="utf-8"
    )
    descriptors = load_tool_descriptors(path)
    assert descriptors[0].model_dump() == {
        "name": "echo",
        "code": "test.echo",
        "description": "回显",
    }


def test_builtin_zoo_searches_without_loading(tmp_path: Path) -> None:
    zoo = ToolZoo()
    register_builtins(zoo, tmp_path)
    assert [item.code for item in zoo.search("文件")] == ["pleiad.tool.read", "pleiad.tool.write"]
    assert zoo.load("pleiad.tool.read").descriptor.name == "read"
    assert zoo.builtin_codes() == (
        "pleiad.tool.bash",
        "pleiad.tool.datetime",
        "pleiad.tool.read",
        "pleiad.tool.write",
    )
    assert [tool.descriptor.name for tool in zoo.builtin_tools()] == [
        "bash",
        "get_datetime",
        "read",
        "write",
    ]


def test_tool_search_ranks_partial_mixed_language_queries(tmp_path: Path) -> None:
    zoo = ToolZoo()
    register_builtins(zoo, tmp_path)

    matches = zoo.search("读取并检查工作区文件")

    assert matches
    assert matches[0].code == "pleiad.tool.read"


def test_datetime_builtin_returns_current_clock_data(tmp_path: Path) -> None:
    zoo = ToolZoo()
    register_builtins(zoo, tmp_path)

    result = asyncio.run(
        ToolRuntime().execute(
            zoo.load("pleiad.tool.datetime"),
            ToolCall(call_id="1", tool_code="pleiad.tool.datetime"),
            ToolContext(execution_id="e", agent_run_id="a"),
        )
    )

    assert result.ok
    assert result.output
    assert set(result.output) == {"iso", "date", "time", "timezone", "unix_timestamp"}


def test_workspace_tool_is_registered_then_published_by_yaml() -> None:
    project_root = Path(__file__).resolve().parents[1]
    workspace = project_root / "workspace"
    zoo = ToolZoo()
    register_builtins(zoo, workspace)
    register_workspace_tools(zoo, workspace)
    for descriptor in load_tool_descriptors(workspace / "capabilities.yaml"):
        zoo.register_descriptor(descriptor)

    assert "example.tool.text_statistics" not in zoo.builtin_codes()
    assert [item.code for item in zoo.search("统计 文本")] == ["example.tool.text_statistics"]

    result = asyncio.run(
        ToolRuntime().execute(
            zoo.load("example.tool.text_statistics"),
            ToolCall(
                call_id="custom-1",
                tool_code="example.tool.text_statistics",
                arguments={"path": "README.md"},
            ),
            ToolContext(execution_id="e", agent_run_id="a"),
        )
    )

    assert result.ok
    assert result.output
    assert result.output["path"] == "README.md"
    assert int(result.output["lines"]) > 0


def test_tool_runtime_rejects_invalid_input(tmp_path: Path) -> None:
    zoo = ToolZoo()
    register_builtins(zoo, tmp_path)
    result = asyncio.run(
        ToolRuntime().execute(
            zoo.load("pleiad.tool.read"),
            ToolCall(call_id="1", tool_code="pleiad.tool.read", arguments={}),
            ToolContext(execution_id="e", agent_run_id="a"),
        )
    )
    assert not result.ok
    assert result.error and result.error.code == "tool.invalid_arguments"


def test_function_tool_is_a_base_tool() -> None:
    async def execute(context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
        return {"value": arguments["value"]}

    tool = FunctionTool(
        ToolDescriptor(name="echo", code="test.echo", description="回显"),
        execute,
        {
            "type": "object",
            "properties": {"value": {}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    result = asyncio.run(
        ToolRuntime().execute(
            tool,
            ToolCall(call_id="1", tool_code="test.echo", arguments={"value": 3}),
            ToolContext(execution_id="e", agent_run_id="a"),
        )
    )
    assert result.ok and result.output == {"value": 3}


def test_non_idempotent_tool_is_never_automatically_retried() -> None:
    attempts = 0

    async def execute(context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("一次性副作用失败")

    tool = FunctionTool(
        ToolDescriptor(name="side-effect", code="test.side_effect", description="一次性操作"),
        execute,
        {"type": "object"},
        policy=ToolPolicy(max_retries=3, idempotent=False),
    )
    result = asyncio.run(
        ToolRuntime().execute(
            tool,
            ToolCall(call_id="1", tool_code="test.side_effect"),
            ToolContext(execution_id="e", agent_run_id="a"),
        )
    )
    assert not result.ok
    assert attempts == 1
