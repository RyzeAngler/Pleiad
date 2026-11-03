from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import ContentSwitcher, Tab, Tabs

from pleiad_weave.tui.state import TuiState
from pleiad_weave.tui.widgets.graph import GraphView
from pleiad_weave.tui.widgets.runtime import RuntimeView
from pleiad_weave.tui.widgets.trace import TraceView


class InspectorPane(Vertical):
    """Host the mutually exclusive Runtime, Graph, and Trace views."""

    active_view: str = "runtime"
    state: TuiState | None = None

    def compose(self) -> ComposeResult:
        yield Tabs(
            Tab("RUNTIME", id="runtime"),
            Tab("GRAPH", id="graph"),
            Tab("TRACE", id="trace"),
            id="inspector-tabs",
        )
        yield ContentSwitcher(
            RuntimeView(id="runtime-view"),
            GraphView(id="graph-view"),
            TraceView(id="trace-view"),
            id="inspector-switcher",
            initial="runtime-view",
        )

    def update_state(self, state: TuiState) -> None:
        """Push the same presentation state into each exclusive inspector view."""
        self.state = state
        self.query_one(RuntimeView).update_state(state)
        self.query_one(GraphView).update_state(state)
        self.query_one(TraceView).update_state(state)

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """Switch the ContentSwitcher when a user clicks an Inspector tab."""
        if event.tabs.id == "inspector-tabs":
            self.show_view(event.tab.id or "runtime")

    def show_view(self, view: str) -> None:
        """Select one Inspector panel by its stable view name."""
        selected = view if view in {"runtime", "graph", "trace"} else "runtime"
        self.active_view = selected
        self.query_one("#inspector-tabs", Tabs).active = selected
        self.query_one("#inspector-switcher", ContentSwitcher).current = f"{selected}-view"
