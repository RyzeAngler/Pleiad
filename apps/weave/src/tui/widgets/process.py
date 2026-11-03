from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from pleiad_weave.tui.state import ProcessGroupView, ProcessItemView, TuiState


class ProcessPane(Vertical):
    """Show the bounded live Process Dock above the Composer."""

    def compose(self) -> ComposeResult:
        yield Static(id="process-header")
        yield RichLog(
            id="process-log",
            max_lines=8,
            markup=False,
            wrap=True,
            highlight=False,
            auto_scroll=True,
        )

    def update_state(self, state: TuiState) -> None:
        """Render active process items and hide the dock when execution is idle."""
        process = state.active_process
        self.display = process is not None
        if process is None:
            return
        self.query_one("#process-header", Static).update(render_process_header(process))
        log = self.query_one("#process-log", RichLog)
        follow_tail = log.scroll_y >= log.max_scroll_y - 1
        log.clear()
        items = process.items[-6:]
        omitted = len(process.items) - len(items)
        if omitted > 0:
            log.write(Text(f"… {omitted} earlier steps", style="#68727C"), scroll_end=follow_tail)
        if not items:
            log.write(Text("Preparing…", style="#A0A9B2"), scroll_end=True)
        for item in items:
            for line in render_process_item(item):
                log.write(line, scroll_end=follow_tail)


def render_process_header(process: ProcessGroupView) -> Text:
    """Render the live Process Dock identity and current phase."""
    text = Text()
    text.append("✦ WEAVE", style="bold #5BD6E8")
    text.append(" · working  ", style="#68727C")
    text.append(process.phase, style="#EAB308")
    return text


def render_process_item(item: ProcessItemView) -> list[Text]:
    """Render one safe process summary or Tool status block."""
    if item.kind == "tool":
        code = f" · {item.tool_code}" if item.tool_code else ""
        first = Text(f"│ {item.title}{code}  ", style="#A0A9B2")
        first.append(item.status.upper(), style=process_status_color(item.status))
        lines = [first]
        if item.detail:
            lines.append(Text(f"└ {item.detail}", style="#A0A9B2"))
        return lines
    return [Text(f"│ {item.content or item.title}", style="#A0A9B2")]


def process_status_color(status: str) -> str:
    """Return the semantic color for a live process status."""
    return {
        "running": "#EAB308",
        "waiting approval": "#7FB7FF",
        "completed": "#56D364",
        "failed": "#EF6B73",
        "cancelled": "#EAB308",
    }.get(status, "#A0A9B2")
