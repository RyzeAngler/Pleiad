"""TUI state projection and Textual pilot tests."""

import asyncio
from collections.abc import Callable

from rich.text import Text
from textual.widgets import Input, RichLog, Static

from pleiad.application.models import ExecuteRequest
from pleiad.observability.models import TraceRecord
from pleiad.runtime.events import RuntimeEvent
from pleiad.storage.models import ExecutionRecord, ExecutionStatus
from pleiad.tui.app import PleiadApp
from pleiad.tui.projection import RuntimeProjection
from pleiad.tui.state import TuiExecutionStatus, TuiState
from pleiad.tui.widgets.conversation import ConversationPane
from pleiad.tui.widgets.runtime import RuntimePane, _render_runtime


class FakeTuiService:
    """Small Application Service double used by the Textual tests."""

    def __init__(self) -> None:
        self.listener: Callable[[RuntimeEvent], None] | None = None

    def subscribe_events(self, listener: Callable[[RuntimeEvent], None]) -> None:
        self.listener = listener

    def unsubscribe_events(self, listener: Callable[[RuntimeEvent], None]) -> None:
        if self.listener is listener:
            self.listener = None

    async def execute(self, request: ExecuteRequest) -> ExecutionRecord:
        execution_id = "exec-tui-test"
        assert self.listener is not None
        self.listener(RuntimeEvent.create(execution_id, "execution.started", sequence=1))
        self.listener(
            RuntimeEvent.create(
                execution_id,
                "graph.revision.accepted",
                {
                    "revision": 1,
                    "nodes": [{"node_id": "search", "objective": "search"}],
                    "edges": [],
                },
                sequence=2,
            )
        )
        self.listener(
            RuntimeEvent.create(
                execution_id,
                "node.started",
                {"node_id": "search"},
                sequence=3,
            )
        )
        self.listener(
            RuntimeEvent.create(
                execution_id,
                "node.succeeded",
                {"node_id": "search", "status": "succeeded"},
                sequence=4,
            )
        )
        self.listener(
            RuntimeEvent.create(
                execution_id,
                "execution.completed",
                {"status": "succeeded"},
                sequence=5,
            )
        )
        return ExecutionRecord(
            execution_id=execution_id,
            conversation_id=request.conversation_id,
            objective=request.objective,
            status=ExecutionStatus.SUCCEEDED,
            graph_id="graph-tui-test",
            graph_revision=1,
            output={"text": "# done"},
        )

    def traces(self, execution_id: str) -> tuple[TraceRecord, ...]:
        return ()

    def get(self, execution_id: str) -> ExecutionRecord | None:
        return None

    def events(self, execution_id: str) -> tuple[RuntimeEvent, ...]:
        return ()


class ComplexGraphTuiService(FakeTuiService):
    """Emit one four-node fan-in Graph for the TUI acceptance test."""

    async def execute(self, request: ExecuteRequest) -> ExecutionRecord:
        execution_id = "exec-complex-graph"
        assert self.listener is not None
        nodes = [
            {"node_id": "extract_north", "objective": "提取北区订单"},
            {"node_id": "extract_south", "objective": "提取南区订单"},
            {"node_id": "extract_rules", "objective": "提取审计规则"},
            {"node_id": "audit_orders", "objective": "汇总并审计订单"},
        ]
        edges = [
            {
                "edge_id": "north_to_audit",
                "source_node_id": "extract_north",
                "target_node_id": "audit_orders",
            },
            {
                "edge_id": "south_to_audit",
                "source_node_id": "extract_south",
                "target_node_id": "audit_orders",
            },
            {
                "edge_id": "rules_to_audit",
                "source_node_id": "extract_rules",
                "target_node_id": "audit_orders",
            },
        ]
        events: list[tuple[str, dict[str, object]]] = [
            ("execution.started", {}),
            ("graph.revision.accepted", {"revision": 1, "nodes": nodes, "edges": edges}),
            ("node.ready", {"node_id": "extract_north"}),
            ("node.ready", {"node_id": "extract_south"}),
            ("node.ready", {"node_id": "extract_rules"}),
            ("node.started", {"node_id": "extract_north"}),
            ("node.started", {"node_id": "extract_south"}),
            ("node.started", {"node_id": "extract_rules"}),
        ]
        for index, node_id in enumerate(
            ("extract_north", "extract_south", "extract_rules"), start=1
        ):
            events.extend(
                [
                    (
                        "tool.started",
                        {
                            "tool_name": "text_statistics",
                            "tool_code": "example.tool.text_statistics",
                            "call_id": f"statistics-{index}",
                            "node_id": node_id,
                        },
                    ),
                    (
                        "tool.succeeded",
                        {"call_id": f"statistics-{index}", "elapsed_ms": 8},
                    ),
                    ("node.succeeded", {"node_id": node_id, "status": "succeeded"}),
                ]
            )
        events.extend(
            [
                ("node.ready", {"node_id": "audit_orders"}),
                ("node.started", {"node_id": "audit_orders"}),
                ("node.succeeded", {"node_id": "audit_orders", "status": "succeeded"}),
                ("graph.completed", {"status": "succeeded"}),
                ("execution.completed", {"status": "succeeded"}),
            ]
        )
        for sequence, (event_type, data) in enumerate(events, start=1):
            self.listener(
                RuntimeEvent.create(
                    execution_id,
                    event_type,
                    data,
                    sequence=sequence,
                )
            )
        return ExecutionRecord(
            execution_id=execution_id,
            conversation_id=request.conversation_id,
            objective=request.objective,
            status=ExecutionStatus.SUCCEEDED,
            graph_id="order-audit",
            graph_revision=1,
            output={"text": "审计完成: 4 个节点, 3 条依赖边。"},
        )


def test_runtime_projection_is_idempotent() -> None:
    state = TuiState()
    state.begin_execution("default")
    state.begin_pleiad_message()
    projection = RuntimeProjection(state)
    event = RuntimeEvent.create(
        "exec-1",
        "graph.revision.accepted",
        {
            "revision": 2,
            "nodes": [{"node_id": "one", "objective": "first"}],
            "edges": [],
        },
        sequence=1,
    )

    assert projection.apply(event) is True
    assert projection.apply(event) is False
    assert state.graph_revision == "2"
    assert state.node_order == ["one"]
    assert state.nodes["one"].objective == "first"

    delta = RuntimeEvent.create(
        "exec-1",
        "agent.text.delta",
        {"delta": "实时回复"},
        sequence=2,
    )
    assert projection.apply(delta) is True
    assert state.messages[0].content == "实时回复"

    thinking = RuntimeEvent.create(
        "exec-1",
        "agent.thinking.delta",
        {"run_id": "agent-1", "delta": "先检查能力"},
        sequence=3,
    )
    response = RuntimeEvent.create(
        "exec-1",
        "agent.response",
        {"run_id": "agent-1", "has_tool_calls": True},
        sequence=4,
    )
    tool_started = RuntimeEvent.create(
        "exec-1",
        "tool.started",
        {
            "tool_name": "get_datetime",
            "tool_code": "pleiad.tool.datetime",
            "call_id": "call-1",
        },
        sequence=5,
    )
    tool_succeeded = RuntimeEvent.create(
        "exec-1",
        "tool.succeeded",
        {"call_id": "call-1", "elapsed_ms": 12},
        sequence=6,
    )
    for runtime_event in (thinking, response, tool_started, tool_succeeded):
        assert projection.apply(runtime_event) is True

    assert state.process_items[0].content == "先检查能力"
    assert state.process_items[0].status == "completed"
    assert state.process_items[1].title == "get_datetime"
    assert state.process_items[1].status == "completed"
    assert state.process_items[1].detail == "12 ms"


def test_tui_submits_objective_and_renders_terminal_state() -> None:
    async def exercise() -> None:
        service = FakeTuiService()
        app = PleiadApp(service)
        async with app.run_test(size=(160, 48)) as pilot:
            input_widget = app.query_one("#composer-input", Input)
            app.set_focus(input_widget)
            input_widget.value = "run task"
            await pilot.press("enter")
            await pilot.pause(0.3)

            assert app.state.execution_status.value == "succeeded"
            assert app.state.graph_revision == "1"
            assert app.state.node_order == ["search"]
            assert app.state.messages[-1].content == "# done"
            assert str(app.query_one("#conversation-header", Static).renderable) == "Conversation"
            assert str(app.query_one("#process-header", Static).renderable) == "Thinking Process"
            runtime_body = app.query_one("#runtime-body", Static).renderable
            assert isinstance(runtime_body, Text)
            assert "search" in runtime_body.plain

    asyncio.run(exercise())


def test_runtime_explains_direct_agent_execution_without_a_graph() -> None:
    state = TuiState(execution_status=TuiExecutionStatus.SUCCEEDED)

    rendered = _render_runtime(state)

    assert "Plan" in rendered.plain
    assert "did not require a Graph" in rendered.plain


def test_tui_renders_complex_parallel_graph_and_custom_tool_activity() -> None:
    async def exercise() -> None:
        app = PleiadApp(ComplexGraphTuiService())
        async with app.run_test(size=(160, 48)) as pilot:
            input_widget = app.query_one("#composer-input", Input)
            app.set_focus(input_widget)
            input_widget.value = "审计两个区域的订单并生成任务图"
            await pilot.press("enter")
            await pilot.pause(0.3)

            assert app.state.execution_status.value == "succeeded"
            assert app.state.node_order == [
                "extract_north",
                "extract_south",
                "extract_rules",
                "audit_orders",
            ]
            assert len(app.state.edges) == 3
            assert all(node.status == "succeeded" for node in app.state.ordered_nodes)
            assert [item.tool_code for item in app.state.process_items] == [
                "example.tool.text_statistics",
                "example.tool.text_statistics",
                "example.tool.text_statistics",
            ]

            runtime = app.query_one(RuntimePane)
            runtime.show_tab("graph")
            await pilot.pause()
            graph_body = app.query_one("#runtime-body", Static).renderable
            assert isinstance(graph_body, Text)
            assert graph_body.plain.count("└─ audit_orders") == 3

    asyncio.run(exercise())


def test_tui_uses_pleiad_identity_and_separates_conversation_turns() -> None:
    async def exercise() -> None:
        app = PleiadApp(FakeTuiService())
        async with app.run_test(size=(160, 48)):
            app.state.messages.clear()
            app.state.add_user_message("first question")
            app.state.begin_pleiad_message()
            app.state.complete_pleiad_message("first answer")
            app.state.add_user_message("second question")
            app.state.begin_pleiad_message()
            app.state.complete_pleiad_message("second answer")

            pane = app.query_one(ConversationPane)
            pane.update_state(app.state)

            lines = [line.text.rstrip() for line in app.query_one(RichLog).lines]
            user_lines = [index for index, line in enumerate(lines) if line == "User:"]
            header = app.query_one("#header", Static).renderable
            composer_label = app.query_one("#composer-label", Static).renderable

            assert isinstance(header, Text)
            assert header.plain.splitlines()[0].startswith("✦ Pleiad  |  model:")
            assert str(composer_label) == "Pleiad>"
            assert lines.count("Pleiad:") == 2
            assert len(user_lines) == 2
            assert lines[user_lines[1] - 1] == ""

    asyncio.run(exercise())
