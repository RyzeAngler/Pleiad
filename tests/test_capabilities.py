"""Workspace capability hot-reload tests."""

import asyncio
from pathlib import Path

from pleiad.runtime.capabilities import WorkspaceCapabilitySync
from pleiad.runtime.events import EventBus
from pleiad.skills.contracts import Skill, SkillDescriptor
from pleiad.skills.zoo import SkillZoo
from pleiad.tools.builtins import register_builtins
from pleiad.tools.contracts import ToolCall, ToolContext
from pleiad.tools.runtime import ToolRuntime
from pleiad.tools.zoo import ToolZoo


def test_skill_search_ranks_partial_queries_without_loading_skill_bodies() -> None:
    zoo = SkillZoo()
    zoo.register(
        SkillDescriptor(
            name="data-audit",
            code="test.data-audit",
            description="依据确定性工具结果完成数据质量审计",
        ),
        lambda descriptor: Skill(descriptor=descriptor, instructions="instructions"),
    )
    zoo.register(
        SkillDescriptor(
            name="writing",
            code="test.writing",
            description="组织正式文档和最终回答",
        ),
        lambda descriptor: Skill(descriptor=descriptor, instructions="instructions"),
    )

    matches = zoo.search("检查数据质量和异常记录")

    assert matches
    assert matches[0].code == "test.data-audit"


def test_workspace_capabilities_are_hot_reloaded_and_keep_last_valid_version(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "v1")
    events = EventBus()
    tools = ToolZoo()
    register_builtins(tools, workspace)
    skills = SkillZoo()
    sync = WorkspaceCapabilitySync(
        tools,
        skills,
        workspace,
        workspace / "capabilities.yaml",
        events,
    )

    first = sync.sync(execution_id="reload-1")

    assert first.applied
    assert first.added_tools == ("test.dynamic",)
    assert first.added_skills == ("test.dynamic-skill",)
    assert skills.load("test.dynamic-skill").instructions == "版本 v1"
    assert _execute_dynamic_tool(tools, "reload-1") == "v1"

    (workspace / "tools" / "dynamic.py").write_text(
        _dynamic_tool_source("v2"),
        encoding="utf-8",
    )
    (workspace / "skills" / "dynamic-skill" / "SKILL.md").write_text(
        "版本 v2",
        encoding="utf-8",
    )
    second = sync.sync(execution_id="reload-2")

    assert second.applied
    assert skills.load("test.dynamic-skill").instructions == "版本 v2"
    assert _execute_dynamic_tool(tools, "reload-2") == "v2"

    (workspace / "tools" / "__init__.py").write_text(
        "def register_tools(:\n",
        encoding="utf-8",
    )
    failed = sync.sync(execution_id="reload-3")

    assert not failed.applied
    assert failed.error is not None
    assert failed.error.code == "system.capability_reload_error"
    assert _execute_dynamic_tool(tools, "reload-3") == "v2"
    assert events.list("reload-3")[-1].event_type == "capabilities.reload_failed"

    (workspace / "tools" / "__init__.py").write_text(
        _workspace_tools_init(),
        encoding="utf-8",
    )
    recovered = sync.sync(execution_id="reload-4")

    assert recovered.applied
    assert events.list("reload-4")[-1].event_type == "capabilities.reloaded"


def _workspace(tmp_path: Path, version: str) -> Path:
    workspace = tmp_path / "workspace"
    tools = workspace / "tools"
    skill = workspace / "skills" / "dynamic-skill"
    tools.mkdir(parents=True)
    skill.mkdir(parents=True)
    (tools / "__init__.py").write_text(_workspace_tools_init(), encoding="utf-8")
    (tools / "dynamic.py").write_text(_dynamic_tool_source(version), encoding="utf-8")
    (skill / "SKILL.md").write_text(f"版本 {version}", encoding="utf-8")
    (workspace / "capabilities.yaml").write_text(
        """tools:
  - name: dynamic
    code: test.dynamic
    description: 动态测试 Tool

skills:
  - name: dynamic-skill
    code: test.dynamic-skill
    description: 动态测试 Skill
""",
        encoding="utf-8",
    )
    return workspace


def _workspace_tools_init() -> str:
    return """from pathlib import Path

from pleiad.tools import BaseTool, ToolDescriptor, ToolZoo

from .dynamic import DynamicTool


def register_tools(zoo: ToolZoo, workspace: Path) -> None:
    def create(descriptor: ToolDescriptor) -> BaseTool:
        return DynamicTool(descriptor, workspace)

    zoo.register_factory("test.dynamic", create)
"""


def _dynamic_tool_source(version: str) -> str:
    return f'''from pathlib import Path

from pleiad.contracts import JsonObject, JsonSchema
from pleiad.tools import BaseTool, ToolContext, ToolDescriptor


class DynamicTool(BaseTool):
    def __init__(self, descriptor: ToolDescriptor, workspace: Path) -> None:
        self._descriptor = descriptor
        self._workspace = workspace

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    @property
    def input_schema(self) -> JsonSchema:
        return {{"type": "object", "additionalProperties": False}}

    @property
    def output_schema(self) -> JsonSchema:
        return {{"type": "object", "required": ["version"]}}

    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        del context, arguments, self._workspace
        return {{"version": "{version}"}}
'''


def _execute_dynamic_tool(tools: ToolZoo, execution_id: str) -> str:
    result = asyncio.run(
        ToolRuntime().execute(
            tools.load("test.dynamic"),
            ToolCall(call_id=execution_id, tool_code="test.dynamic"),
            ToolContext(execution_id=execution_id, agent_run_id="agent"),
        )
    )
    assert result.ok
    assert result.output is not None
    return str(result.output["version"])
