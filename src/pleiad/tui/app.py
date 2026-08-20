"""Textual application for the Pleiad terminal workbench."""

import asyncio
from typing import Any

from textual.app import App, ComposeResult
from textual.events import Resize
from textual.widgets import Input

from pleiad.application.models import ExecuteRequest
from pleiad.application.service import ExecutionService
from pleiad.runtime.events import RuntimeEvent
from pleiad.tui.messages import RuntimeEventMessage
from pleiad.tui.projection import RuntimeProjection
from pleiad.tui.screens.help import HelpScreen
from pleiad.tui.screens.main import MainScreen
from pleiad.tui.screens.trace import TraceScreen
from pleiad.tui.state import TuiExecutionStatus, TuiState
from pleiad.tui.widgets.composer import Composer
from pleiad.tui.widgets.conversation import ConversationPane
from pleiad.tui.widgets.footer import StatusFooter
from pleiad.tui.widgets.header import HeaderBar
from pleiad.tui.widgets.runtime import RuntimePane


class PleiadApp(App[None]):
    """Conversation-first TUI that talks only to Application Service boundaries."""

    CSS_PATH = "theme.tcss"
    TITLE = "Pleiad"

    BINDINGS = [  # noqa: RUF012
        ("ctrl+c", "quit_or_cancel", "Exit / Cancel"),
        ("escape", "cancel_execution", "Cancel"),
        ("ctrl+k", "clear_input", "Clear Input"),
        ("ctrl+l", "clear_screen", "Clear Screen"),
        ("g", "show_graph", "Show Graph"),
        ("r", "show_runtime", "Show Runtime"),
        ("t", "open_trace", "Open Trace"),
    ]

    def __init__(self, service: ExecutionService) -> None:
        super().__init__()
        self.service = service
        self.state = TuiState(model_label=_model_label(service))
        self.projection = RuntimeProjection(self.state)
        self._execution_worker: Any = None

    def compose(self) -> ComposeResult:
        yield MainScreen()

    def on_mount(self) -> None:
        """Subscribe before accepting input and render the empty state."""
        self.service.subscribe_events(self._on_runtime_event)
        self._sync_ui()

    def on_unmount(self) -> None:
        """Detach the EventBus listener when the TUI closes."""
        self.service.unsubscribe_events(self._on_runtime_event)

    def on_resize(self, event: Resize) -> None:
        """Switch the main screen to its single-column narrow mode."""
        self.query_one(MainScreen).set_narrow(event.size.width < 100)

    def on_runtime_event_message(self, message: RuntimeEventMessage) -> None:
        """Project one event and refresh only the visible presentation state."""
        if self.projection.apply(message.event):
            self._sync_ui()

    def _on_runtime_event(self, event: RuntimeEvent) -> None:
        """Receive events from any thread and hand them to Textual."""
        self.post_message(RuntimeEventMessage(event))

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        """Handle goals and the small command set defined by the design."""
        value = message.value.strip()
        if value.startswith("/") or value.startswith(":"):
            self._handle_command(value)
            return
        if self.state.input_status != "idle":
            self.notify(
                "An execution is currently in progress. Please wait or cancel it.",
                severity="warning",
            )
            return
        self.state.begin_execution(self.state.conversation_id)
        self.state.add_user_message(value)
        self.state.begin_pleiad_message()
        self._sync_ui()
        self._execution_worker = self.run_worker(
            self._execute(value),
            name="pleiad-execution",
            exclusive=True,
        )

    async def _execute(self, objective: str) -> None:
        """Submit one objective and project its durable terminal record."""
        try:
            record = await self.service.execute(
                ExecuteRequest(
                    objective=objective,
                    conversation_id=self.state.conversation_id,
                )
            )
        except asyncio.CancelledError:
            self.state.execution_status = TuiExecutionStatus.CANCELLED
            self.state.input_status = "idle"
            self.state.complete_pleiad_message("Execution canceled.")
            self._sync_ui()
            return
        except Exception as exc:
            self.state.execution_status = TuiExecutionStatus.FAILED
            self.state.input_status = "idle"
            self.state.add_error_message("SYSTEM_UNEXPECTED", exc.__class__.__name__)
            self._sync_ui()
            return
        self.state.apply_execution_record(record)
        self._sync_ui()

    def action_quit_or_cancel(self) -> None:
        """Exit while idle and request cancellation while an Execution runs."""
        if self.state.execution_status is TuiExecutionStatus.RUNNING:
            self.action_cancel_execution()
            return
        self.exit()

    def action_cancel_execution(self) -> None:
        """Cancel the current Textual worker without inventing a Runtime API."""
        if self.state.execution_status is not TuiExecutionStatus.RUNNING:
            return
        if self._execution_worker is not None:
            self._execution_worker.cancel()
        self.state.input_status = "submitting"
        self._sync_ui()

    def action_clear_input(self) -> None:
        """Clear only the current Composer draft."""
        self.query_one("#composer-input", Input).value = ""

    def action_clear_screen(self) -> None:
        """Clear the visible conversation projection, not durable history."""
        self.state.messages.clear()
        self.state.clear_process()
        self._sync_ui()

    def action_show_graph(self) -> None:
        """Select the Graph tab and reveal Runtime on narrow screens."""
        runtime = self.query_one(RuntimePane)
        runtime.show_tab("graph")
        main = self.query_one(MainScreen)
        main.show_runtime_panel("narrow" in main.classes)

    def action_show_runtime(self) -> None:
        """Select the Runtime tab."""
        self.query_one(RuntimePane).show_tab("runtime")
        main = self.query_one(MainScreen)
        main.show_runtime_panel("narrow" in main.classes)

    def action_open_trace(self) -> None:
        """Open Trace details for the current Execution when available."""
        if self.state.execution_id is None:
            self.notify("No Trace is available for the current execution.", severity="warning")
            return
        self.push_screen(TraceScreen(self.service, self.state.execution_id))

    def _handle_command(self, value: str) -> None:
        command = value.lower()
        if command == "/help":
            self.push_screen(HelpScreen())
        elif command in {":show graph", ":graph"}:
            self.action_show_graph()
        elif command == ":runtime":
            self.action_show_runtime()
        elif command == ":trace":
            self.action_open_trace()
        elif command == ":clear":
            self.action_clear_screen()
        elif command == ":switch model":
            self.notify(
                "Model selection is managed by global configuration; "
                "runtime switching is unavailable.",
                severity="information",
            )
        elif command == ":export log":
            self.notify(
                "Log export will be available after the Trace view is stabilized.",
                severity="information",
            )
        else:
            self.notify(f"Unknown command: {value}", severity="warning")

    def _sync_ui(self) -> None:
        """Push the presentation state into the five main widgets."""
        self.query_one(HeaderBar).update_state(self.state)
        self.query_one(ConversationPane).update_state(self.state)
        self.query_one(RuntimePane).update_state(self.state)
        composer = self.query_one(Composer)
        composer.set_status(self.state.input_status)
        self.query_one(StatusFooter).update_state(self.state)


def _model_label(service: ExecutionService) -> str:
    """Read the global text model label without widening the UI contract."""
    pleiad = getattr(service, "pleiad", None)
    models = getattr(pleiad, "models", None)
    if models is not None:
        try:
            return f"model: {models.resolve('text').profile.model}"
        except (AttributeError, LookupError, TypeError):
            pass
    return "model: unavailable"
