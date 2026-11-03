from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Tab, Tabs

from pleiad_weave.tui.state import TuiState
from pleiad_weave.tui.widgets.composer import Composer
from pleiad_weave.tui.widgets.conversation import ConversationPane
from pleiad_weave.tui.widgets.footer import ModelContextFooter
from pleiad_weave.tui.widgets.header import HeaderBar
from pleiad_weave.tui.widgets.inspector import InspectorPane


class MainScreen(Vertical):
    """Compose the conversation-first wide and narrow terminal layouts."""

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        yield Tabs(
            Tab("CHAT", id="chat"),
            Tab("RUNTIME", id="runtime"),
            Tab("GRAPH", id="graph"),
            Tab("TRACE", id="trace"),
            id="narrow-view-tabs",
        )
        yield Horizontal(
            ConversationPane(id="conversation-pane"),
            InspectorPane(id="inspector-pane"),
            id="workspace",
        )
        yield Composer(id="composer")
        yield ModelContextFooter(id="footer")

    def update_state(self, state: TuiState) -> None:
        """Push presentation state into the four visible regions."""
        self.query_one(ConversationPane).update_state(state)
        self.query_one(InspectorPane).update_state(state)
        self.query_one(Composer).set_status(state.input_status)
        self.query_one(ModelContextFooter).update_state(state)

    def set_width(self, width: int) -> None:
        """Apply narrow and medium layout classes for the current terminal width."""
        narrow = width < 100
        compact = 100 <= width < 120
        changed_narrow = narrow != ("narrow" in self.classes)
        self.set_class(narrow, "narrow")
        self.set_class(compact, "compact")
        if changed_narrow and narrow:
            self.show_view("chat")
        elif not narrow:
            self.remove_class("inspector-visible")

    def set_height(self, height: int) -> None:
        """Reduce optional process space on short terminals."""
        self.set_class(height < 28, "short")

    def show_view(self, view: str) -> None:
        """Select Chat or one Inspector view without destroying any state."""
        if view == "chat":
            self.set_class(False, "inspector-visible")
            self.query_one("#narrow-view-tabs", Tabs).active = "chat"
            return
        selected = view if view in {"runtime", "graph", "trace"} else "runtime"
        self.set_class("narrow" in self.classes, "inspector-visible")
        self.query_one("#narrow-view-tabs", Tabs).active = selected
        self.query_one(InspectorPane).show_view(selected)

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """Route only the narrow navigation tabs to the selected view."""
        if event.tabs.id == "narrow-view-tabs":
            self.show_view(event.tab.id or "chat")
