"""Local Trace inspection screen."""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import RichLog, Static

from pleiad.application.service import ExecutionService
from pleiad.observability.models import TraceRecord


class TraceScreen(Screen[None]):
    """Render Trace records through the Application Service boundary."""

    BINDINGS = [("escape", "app.pop_screen", "Close")]  # noqa: RUF012

    def __init__(self, service: ExecutionService, execution_id: str) -> None:
        super().__init__()
        self.service = service
        self.execution_id = execution_id

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                f"Trace  ·  Execution: {self.execution_id}  ·  Press Esc to close",
                id="trace-title",
            ),
            RichLog(id="trace-log", markup=False, wrap=True),
            id="trace-screen",
        )

    def on_mount(self) -> None:
        """Load local Trace records once when the screen opens."""
        log = self.query_one("#trace-log", RichLog)
        records = self.service.traces(self.execution_id)
        if not records:
            log.write(Text("No Trace records available.", style="#9AA9BB"))
            return
        for record in records:
            log.write(_render_record(record))


def _render_record(record: TraceRecord) -> Text:
    indent = "  " if record.parent_span_id else ""
    text = Text()
    text.append(f"{indent}{record.record_type.value} ", style="#66C8F2")
    text.append(f"{record.kind.value}:{record.name} ", style="#D8E1EC")
    text.append(f"[{record.status.value}]", style="#F0B84B")
    return text
