"""Main Pleiad terminal workbench screen."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical

from pleiad.tui.widgets.composer import Composer
from pleiad.tui.widgets.conversation import ConversationPane
from pleiad.tui.widgets.footer import StatusFooter
from pleiad.tui.widgets.header import HeaderBar
from pleiad.tui.widgets.runtime import RuntimePane


class MainScreen(Vertical):
    """Conversation-first workspace described by the TUI design document."""

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        yield Horizontal(
            ConversationPane(id="conversation-pane"),
            RuntimePane(id="runtime-pane"),
            id="workspace",
        )
        yield Composer(id="composer")
        yield StatusFooter(id="footer")

    def set_narrow(self, narrow: bool) -> None:
        """Apply the single-column mode at small terminal widths."""
        self.set_class(narrow, "narrow")
        if not narrow:
            self.remove_class("runtime-visible")

    def show_runtime_panel(self, visible: bool) -> None:
        """Show Runtime instead of Conversation when a narrow screen requests it."""
        self.set_class(visible, "runtime-visible")
