"""Runtime timeline, Graph view, and Latest Event widgets."""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, Tab, Tabs

from pleiad.tui.state import EventView, NodeView, TuiExecutionStatus, TuiState


class RuntimePane(Vertical):
    """Show the current dynamic Graph execution without scheduling it."""

    def compose(self) -> ComposeResult:
        yield Tabs(
            Tab("Runtime", id="runtime"),
            Tab("Graph", id="graph"),
            id="runtime-tabs",
        )
        yield VerticalScroll(Static(id="runtime-body"), id="runtime-scroll")
        yield Static(id="latest-event")

    _state: TuiState | None = None
    _active_tab = "runtime"

    def update_state(self, state: TuiState) -> None:
        """Render either the Runtime timeline or the Graph structure."""
        self._state = state
        body = self.query_one("#runtime-body", Static)
        body.update(
            _render_runtime(state) if self._active_tab == "runtime" else _render_graph(state)
        )
        latest = self.query_one("#latest-event", Static)
        latest.update(_render_latest_event(state.latest_event))

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """Switch the body while keeping Latest Event fixed."""
        self._active_tab = event.tab.id or "runtime"
        if self._state is not None:
            self.update_state(self._state)

    def show_tab(self, tab: str) -> None:
        """Select Runtime or Graph programmatically."""
        self._active_tab = "graph" if tab == "graph" else "runtime"
        tabs = self.query_one("#runtime-tabs", Tabs)
        tabs.active = self._active_tab
        if self._state is not None:
            self.update_state(self._state)


def _render_runtime(state: TuiState) -> Text:
    text = Text()
    text.append("Execution\n", style="#66C8F2")
    text.append(f"status  {state.execution_status.value}\n", style="#D8E1EC")
    text.append(f"phase   {_runtime_phase(state)}\n\n", style="#D8E1EC")
    text.append("Plan\n", style="#66C8F2")
    if not state.ordered_nodes:
        text.append(_empty_plan_message(state), style="#9AA9BB")
        return text
    graph = state.graph_id or "Unnamed Graph"
    revision = state.graph_revision or "-"
    text.append(f"{graph}  revision {revision}\n\n", style="#9AA9BB")
    for index, node in enumerate(state.ordered_nodes):
        text.append(_node_line(node), style=_node_style(node))
        text.append("\n")
        if index < len(state.ordered_nodes) - 1:
            text.append("│\n", style="#526477")
    return text


def _render_graph(state: TuiState) -> Text:
    text = Text()
    revision = state.graph_revision or "-"
    text.append(f"Graph Revision: {revision}\n\n", style="#66C8F2")
    if not state.ordered_nodes:
        text.append("This execution did not generate a Graph.", style="#9AA9BB")
        return text
    for node in state.ordered_nodes:
        text.append(f"○ {node.name}\n", style="#D8E1EC")
        for edge in state.edges:
            if edge.source_node_id == node.node_id:
                text.append(f"    └─ {edge.target_node_id}\n", style="#788899")
    return text


def _node_line(node: NodeView) -> str:
    duration = _duration(node)
    detail = node.detail or node.error_message or node.status
    return f"{_status_icon(node.status)} {node.name}\n  {duration}\n  {detail}"


def _node_style(node: NodeView) -> str:
    return {
        "running": "#F0B84B",
        "succeeded": "#78D17B",
        "failed": "#F06C75",
        "cancelled": "#F0B84B",
    }.get(node.status, "#9AA9BB")


def _status_icon(status: str) -> str:
    return {
        "running": "●",
        "succeeded": "✓",
        "failed": "✕",
        "cancelled": "-",
        "ready": "◌",
    }.get(status, "○")


def _duration(node: NodeView) -> str:
    if node.duration_ms is not None:
        return f"{node.duration_ms / 1000:.2f} s"
    if node.started_at is not None and node.status == "running":
        return f"since {node.started_at.astimezone().strftime('%H:%M:%S')}"
    return "—"


def _render_latest_event(event: EventView | None) -> Text:
    text = Text("Latest Event\n────────────\n", style="#66C8F2")
    if event is None:
        text.append("No runtime events are available.", style="#9AA9BB")
        return text
    fields = (
        ("time", event.timestamp.astimezone().strftime("%H:%M:%S.%f")[:-3]),
        ("node", event.node_name or "-"),
        ("event", event.event_type),
        ("status", event.status),
        ("detail", event.detail or "-"),
        ("elapsed", f"{event.elapsed_ms} ms" if event.elapsed_ms is not None else "-"),
    )
    for name, value in fields:
        text.append(f"{name:<8}", style="#9AA9BB")
        text.append(f"{value}\n", style="#F0B84B" if name == "status" else "#D8E1EC")
    return text


def _runtime_phase(state: TuiState) -> str:
    event = state.latest_event
    if event is None:
        return "Waiting for execution"
    if event.event_type == "execution.started":
        return "Processing request"
    if event.event_type.startswith("agent."):
        return "Generating response"
    if event.event_type == "graph.revision.accepted":
        return "Graph generated"
    if event.event_type == "graph.started":
        return "Executing Graph"
    if event.event_type.startswith("node."):
        return f"Executing node {event.node_name or '-'}"
    if event.event_type.startswith("tool."):
        return f"Invoking tool {event.detail or '-'}"
    if event.event_type == "execution.completed":
        return "Execution completed"
    if event.event_type == "execution.failed":
        return "Execution failed"
    if event.event_type == "execution.cancelled":
        return "Execution canceled"
    return event.event_type


def _empty_plan_message(state: TuiState) -> str:
    if state.execution_status is TuiExecutionStatus.RUNNING:
        return "The Agent is processing the request; no Graph has been created."
    if state.execution_status is TuiExecutionStatus.SUCCEEDED:
        return "The request did not require a Graph. The Agent completed the response directly."
    if state.execution_status is TuiExecutionStatus.FAILED:
        return "This execution did not produce a displayable Graph."
    if state.execution_status is TuiExecutionStatus.CANCELLED:
        return "Execution canceled before a displayable Graph was created."
    return "Submit a request to view the execution plan."
