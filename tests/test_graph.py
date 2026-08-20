"""Graph validation, revision, and scheduling tests."""

import asyncio
from pathlib import Path

from jsonschema import Draft202012Validator

from pleiad.graph.contracts import EdgeSpec, GraphSpec, NodeSpec
from pleiad.graph.store import GraphConflictError, TaskGraphStore
from pleiad.graph.validator import GraphValidator
from pleiad.runtime.events import EventBus
from pleiad.runtime.pleiad import PleiadAgent, _submit_schema
from pleiad.runtime.scheduler import topological_stages
from pleiad.skills.contracts import Skill, SkillDescriptor
from pleiad.skills.zoo import SkillZoo
from pleiad.tools.base import FunctionTool
from pleiad.tools.contracts import ToolContext, ToolDescriptor
from pleiad.tools.zoo import ToolZoo


def _catalogs() -> tuple[ToolZoo, SkillZoo]:
    async def execute(context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
        return {}

    tools = ToolZoo()
    tools.register(
        ToolDescriptor(name="echo", code="test.echo", description="回显"),
        lambda descriptor: FunctionTool(descriptor, execute, {"type": "object"}),
    )
    skills = SkillZoo()
    descriptor = SkillDescriptor(name="analysis", code="test.analysis", description="分析")
    skills.register(
        descriptor,
        lambda selected: Skill(descriptor=selected, instructions="分析"),
    )
    return tools, skills


def test_validator_rejects_cycle_and_unknown_capability() -> None:
    tools, skills = _catalogs()
    graph = GraphSpec(
        graph_id="g",
        objective="目标",
        nodes=(
            NodeSpec(node_id="a", objective="a", tool_codes=("missing",)),
            NodeSpec(node_id="b", objective="b"),
        ),
        edges=(
            EdgeSpec(edge_id="ab", source_node_id="a", target_node_id="b"),
            EdgeSpec(edge_id="ba", source_node_id="b", target_node_id="a"),
        ),
    )
    result = GraphValidator(tools, skills).validate(graph)
    assert not result.valid
    assert {item.code for item in result.diagnostics} == {"unknown_tool", "cycle"}


def test_graph_revision_uses_compare_and_swap_and_survives_reload(tmp_path: Path) -> None:
    store = TaskGraphStore(tmp_path / "graphs")
    first = GraphSpec(
        graph_id="g",
        revision=1,
        objective="one",
        nodes=(NodeSpec(node_id="n", objective="n"),),
    )
    second = first.model_copy(update={"revision": 2, "objective": "two"})
    store.submit(first)
    store.submit(second, expected_revision=1)
    reloaded = TaskGraphStore(tmp_path / "graphs")
    current = reloaded.current("g")
    assert current is not None
    assert current.graph.objective == "two"
    assert reloaded.revision("g", 1) is not None
    try:
        reloaded.submit(first, expected_revision=1)
    except GraphConflictError:
        pass
    else:
        raise AssertionError("stale graph revision was accepted")


def test_scheduler_returns_parallel_stages() -> None:
    graph = GraphSpec(
        graph_id="g",
        objective="目标",
        nodes=tuple(NodeSpec(node_id=node_id, objective=node_id) for node_id in ("a", "b", "c")),
        edges=(EdgeSpec(edge_id="ac", source_node_id="a", target_node_id="c"),),
    )
    assert [[node.node_id for node in stage] for stage in topological_stages(graph)] == [
        ["a", "b"],
        ["c"],
    ]


def test_submit_graph_tool_exposes_the_complete_graph_schema() -> None:
    schema = _submit_schema()
    graph = GraphSpec(
        graph_id="schema-test",
        objective="验证提交结构",
        nodes=(NodeSpec(node_id="node", objective="执行"),),
    )

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(graph.model_dump(mode="json"))

    errors = list(
        Draft202012Validator(schema).iter_errors(
            {
                **graph.model_dump(mode="json"),
                "nodes": [
                    {
                        **graph.nodes[0].model_dump(mode="json"),
                        "model_role": "extract",
                    }
                ],
            }
        )
    )
    assert errors
    assert "extract" in errors[0].message


def test_submit_graph_returns_actionable_structure_diagnostics() -> None:
    agent = object.__new__(PleiadAgent)
    agent.events = EventBus()
    context = ToolContext(execution_id="graph-invalid", agent_run_id="agent")

    result = asyncio.run(
        agent._submit_graph(
            context,
            {
                "graph_id": "invalid",
                "objective": "验证诊断",
                "nodes": [
                    {
                        "node_id": "node",
                        "objective": "执行",
                        "model_role": "extract",
                    }
                ],
            },
        )
    )

    assert result["accepted"] is False
    diagnostics = result["diagnostics"]
    assert isinstance(diagnostics, list)
    assert diagnostics[0]["path"] == "nodes.0.model_role"
    assert agent.events.list("graph-invalid")[-1].event_type == "graph.revision.rejected"


def test_supervisor_selects_tool_development_skill_for_capability_goals() -> None:
    agent = object.__new__(PleiadAgent)
    agent.skills = SkillZoo()
    descriptor = SkillDescriptor(
        name="tool-development",
        code="pleiad.skill.tool-development",
        description="开发 Tool",
    )
    agent.skills.register(
        descriptor,
        lambda selected: Skill(descriptor=selected, instructions="开发流程"),
    )

    assert agent._supervisor_skill_codes("请开发一个读取订单的工具") == (
        "pleiad.skill.tool-development",
    )
    assert agent._supervisor_skill_codes("请使用工具读取订单") == ()
