from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from pleiad_weave.tui.state import NodeView, TuiState


class GraphView(VerticalScroll):
    """Render Graph structure and state without duplicating Runtime or Trace."""

    def compose(self) -> ComposeResult:
        yield Static(id="graph-body")

    def update_state(self, state: TuiState) -> None:
        """Update the Graph projection while retaining the scroll offset."""
        saved_scroll_y = self.scroll_y
        self.query_one("#graph-body", Static).update(render_graph(state))
        self.scroll_to(y=saved_scroll_y, animate=False, immediate=True)


def render_graph(state: TuiState) -> Text:
    """Render deterministic node order and explicit DAG edges."""
    graph = state.graph
    text = Text()
    revision = graph.revision or "—"
    text.append(f"GRAPH v{revision}", style="bold #5BD6E8")
    text.append(f"    {state.graph_node_count} nodes\n\n", style="#68727C")
    if not graph.ordered_nodes:
        text.append("This execution did not create a Graph.", style="#A0A9B2")
        return text
    for node in graph.ordered_nodes:
        text.append(node_marker(node, state), style=node_color(node.status))
        text.append(f" {node.name}", style="#E0E4E8")
        text.append(f"  {node.status}\n", style=node_color(node.status))
        detail = node.error_code or node.detail
        if detail:
            text.append(f"    {detail}\n", style="#A0A9B2")
    if graph.edges:
        text.append("\nEDGES\n", style="bold #5BD6E8")
        for edge in graph.edges:
            text.append(f"{edge.source_node_id} → {edge.target_node_id}\n", style="#A0A9B2")
    return text


def node_marker(node: NodeView, state: TuiState) -> str:
    """Return a visible marker for node status and active selection."""
    if state.runtime.current_node_name in {node.node_id, node.name} and node.status == "running":
        return "●"
    return {
        "running": "●",
        "succeeded": "✓",
        "failed": "✕",
        "cancelled": "-",
        "ready": "◌",
    }.get(node.status, "○")


def node_color(status: str) -> str:
    """Return the semantic node state color."""
    return {
        "running": "#EAB308",
        "succeeded": "#56D364",
        "failed": "#EF6B73",
        "cancelled": "#EAB308",
        "ready": "#7FB7FF",
    }.get(status, "#A0A9B2")
