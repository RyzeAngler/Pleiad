"""Fixed live Thinking Process window for provider reasoning and Tool execution."""

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from pleiad.tui.state import ProcessItemView, TuiState


class ProcessPane(Vertical):
    """A fixed-height, auto-following window for transient execution activity."""

    def compose(self) -> ComposeResult:
        yield Static("Thinking Process", id="process-header")
        yield RichLog(
            id="process-log",
            markup=False,
            wrap=True,
            highlight=False,
            auto_scroll=True,
        )

    def update_state(self, state: TuiState) -> None:
        """Render the latest thinking and Tool events without fixed narration."""
        log = self.query_one("#process-log", RichLog)
        follow_tail = log.scroll_y >= log.max_scroll_y - 1
        log.clear()
        if not state.process_items:
            log.write(Text("No active thinking process.", style="#788899"), scroll_end=True)
        else:
            for item in state.process_items:
                log.write(_render_item(item), scroll_end=follow_tail)


def _render_item(item: ProcessItemView) -> Group:
    icon, style = {
        "running": ("◌", "#F0B84B"),
        "completed": ("✓", "#78D17B"),
        "failed": ("✕", "#F06C75"),
        "cancelled": ("⊘", "#F0B84B"),
    }.get(item.status, ("·", "#9AA9BB"))
    heading = Text()
    heading.append(f"{icon} ", style=style)
    heading.append(item.title, style=f"bold {style}")
    if item.tool_code:
        heading.append(f"  {item.tool_code}", style="#9AA9BB")
    if item.detail:
        heading.append(f"  {item.detail}", style="#9AA9BB")
    if not item.content:
        return Group(heading)
    return Group(heading, Text(item.content, style="#D8E1EC"))
