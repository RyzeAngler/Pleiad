"""Bottom command and system status bar."""

from rich.text import Text
from textual.widgets import Static

from pleiad.tui.state import TuiState


class StatusFooter(Static):
    """Render keyboard commands and Logger/Trace health."""

    def update_state(self, state: TuiState) -> None:
        """Update the footer from presentation state."""
        text = Text()
        text.append("^K", style="#66C8F2")
        text.append(" clear input   ", style="#9AA9BB")
        text.append("^L", style="#66C8F2")
        text.append(" clear screen   ", style="#9AA9BB")
        text.append("/help   :show graph   :trace", style="#9AA9BB")
        text.append("\n")
        text.append(f"logger: {state.logger_status}   trace: {state.trace_status}", style="#78D17B")
        self.update(text)
