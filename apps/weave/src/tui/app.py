import asyncio
import logging
from typing import Literal

from textual.app import App, ComposeResult
from textual.events import Resize
from textual.widgets import Input
from textual.worker import Worker

from pleiad_weave.application.dto import ExecuteRequest
from pleiad_weave.application.events import ExecutionEvent
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.storage.models import ExecutionStatus
from pleiad_weave.tui.log_bridge import TuiLogHandler
from pleiad_weave.tui.messages import ExecutionEventMessage, ProgramLogMessage
from pleiad_weave.tui.projection import TuiProjection
from pleiad_weave.tui.screens.approvals import ApprovalScreen, ApprovalSelection
from pleiad_weave.tui.screens.help import HelpScreen
from pleiad_weave.tui.screens.main import MainScreen
from pleiad_weave.tui.state import (
    InputStatus,
    LogRecordView,
    ModelContextView,
    TuiExecutionStatus,
    TuiState,
)
from pleiad_weave.tui.widgets.composer import Composer


class PleiadApp(App[None]):
    """Conversation-first TUI backed by the Weave Application Service."""

    CSS_PATH = "styles/theme.tcss"
    TITLE = "Pleiad Weave"

    BINDINGS = [  # noqa: RUF012
        ("ctrl+c", "quit_or_cancel", "Exit / Cancel"),
        ("escape", "cancel_execution", "Cancel"),
        ("ctrl+k", "clear_input", "Clear Input"),
        ("ctrl+l", "clear_screen", "Clear Screen"),
        ("g", "show_graph", "Show Graph"),
        ("r", "show_runtime", "Show Runtime"),
        ("t", "open_trace", "Show Trace"),
        ("?", "open_help", "Help"),
    ]

    def __init__(self, service: ExecutionService) -> None:
        super().__init__()
        self.service = service
        self.main_view = MainScreen()
        self.state = TuiState(model_context=model_context_from_service(service))
        self.projection = TuiProjection(self.state)
        self.execution_worker: Worker[None] | None = None
        self.cancel_worker: Worker[None] | None = None
        self.cancel_requested = False
        self.program_log_handler = TuiLogHandler(self.receive_log_record)
        self.program_log_loggers: tuple[logging.Logger, ...] = ()

    def compose(self) -> ComposeResult:
        yield self.main_view

    async def on_mount(self) -> None:
        """Install UI feeds before startup recovery and focus the Composer."""
        self.install_program_log_handler()
        self.service.subscribe_events(self.receive_execution_event)
        await self.service.start()
        main = self.main_view
        main.set_width(self.size.width)
        main.set_height(self.size.height)
        self.sync_ui()
        self.main_view.query_one("#composer-input", Input).focus()
        await self.show_approvals(notify_empty=False)

    async def on_unmount(self) -> None:
        """Detach UI feeds before stopping the Application Service."""
        self.remove_program_log_handler()
        self.service.unsubscribe_events(self.receive_execution_event)
        await self.service.shutdown()

    def on_resize(self, event: Resize) -> None:
        """Apply responsive width and short-terminal height classes."""
        main = self.main_view
        main.set_width(event.size.width)
        main.set_height(event.size.height)

    async def on_execution_event_message(self, message: ExecutionEventMessage) -> None:
        """Project one ordered event and refresh the affected presentation."""
        if self.projection.apply(message.event):
            self.sync_ui()
        if isinstance(self.screen, ApprovalScreen) and message.event.event_type in {
            "execution.waiting_approval",
            "execution.resume_queued",
            "execution.resumed",
            "execution.completed",
            "execution.failed",
            "execution.cancelled",
        }:
            await self.refresh_approvals()

    def on_program_log_message(self, message: ProgramLogMessage) -> None:
        """Append one formatted Program Log record to the bounded feed."""
        self.state.add_program_log(message.record)
        self.sync_ui()

    def receive_execution_event(self, event: ExecutionEvent) -> None:
        """Forward an event from the service thread into Textual's event loop."""
        self.post_message(ExecutionEventMessage(event))

    def receive_log_record(self, record: LogRecordView) -> None:
        """Forward one TUI-safe log view into Textual's event loop."""
        self.post_message(ProgramLogMessage(record))

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        """Handle an objective or a command submitted from the Composer."""
        value = message.value.strip()
        if value.startswith("/") or value.startswith(":"):
            self.handle_command(value)
            return
        if self.state.input_status is not InputStatus.IDLE:
            self.notify(
                "An execution is in progress. Use /approvals to decide a pending action, or cancel it.",
                severity="warning",
            )
            return
        self.state.begin_execution(self.state.conversation_id)
        self.state.add_user_message(value)
        self.main_view.query_one(Composer).clear_input()
        self.cancel_requested = False
        self.sync_ui()
        self.execution_worker = self.run_worker(
            self.execute(value),
            name="pleiad-execution",
            exclusive=True,
        )

    async def execute(self, objective: str) -> None:
        """Submit one objective and apply its durable terminal record."""
        execution_id: str | None = None
        try:
            queued = await self.service.submit(
                ExecuteRequest(
                    objective=objective,
                    conversation_id=self.state.conversation_id,
                )
            )
            execution_id = queued.execution_id
            self.state.execution_id = execution_id
            for event in await self.service.events(execution_id, after_sequence=0):
                self.projection.apply(event)
            self.sync_ui()
            if self.cancel_requested:
                record = await self.service.cancel(execution_id)
            else:
                record = await self.service.wait(execution_id)
            if record is None:
                raise LookupError(f"execution not found: {execution_id}")
        except asyncio.CancelledError:
            if execution_id is None:
                self.state.execution_status = TuiExecutionStatus.CANCELLED
                self.state.input_status = InputStatus.IDLE
                self.state.add_system_notice("Execution cancelled.", severity="warning")
            else:
                record = await asyncio.shield(self.service.cancel(execution_id))
                if record is not None:
                    self.state.apply_execution_record(record)
            self.sync_ui()
            self.focus_composer()
            return
        except Exception as exc:
            self.state.execution_status = TuiExecutionStatus.FAILED
            self.state.input_status = InputStatus.ERROR
            self.state.add_error_message("SYSTEM_UNEXPECTED", exc.__class__.__name__)
            self.sync_ui()
            self.focus_composer()
            return
        self.state.apply_execution_record(record)
        self.sync_ui()
        self.focus_composer()
        if record.status is ExecutionStatus.WAITING_APPROVAL:
            await self.show_approvals(record.execution_id)

    def action_quit_or_cancel(self) -> None:
        """Exit while idle or request cancellation while an execution is active."""
        if self.state.execution_status in {
            TuiExecutionStatus.RUNNING,
            TuiExecutionStatus.QUEUED,
            TuiExecutionStatus.WAITING_APPROVAL,
            TuiExecutionStatus.RESUME_QUEUED,
        }:
            self.request_execution_cancellation(return_to_chat=False)
            return
        self.exit()

    def action_cancel_execution(self) -> None:
        """Ask the Application Service to cancel the current execution."""
        self.request_execution_cancellation(return_to_chat=True)

    def request_execution_cancellation(self, *, return_to_chat: bool) -> None:
        """Request cancellation and optionally leave a narrow Inspector view first."""
        main = self.main_view
        if return_to_chat and "narrow" in main.classes and "inspector-visible" in main.classes:
            main.show_view("chat")
            return
        if self.state.execution_status not in {
            TuiExecutionStatus.RUNNING,
            TuiExecutionStatus.QUEUED,
            TuiExecutionStatus.WAITING_APPROVAL,
            TuiExecutionStatus.RESUME_QUEUED,
        }:
            return
        self.cancel_requested = True
        self.state.input_status = InputStatus.SUBMITTING
        self.sync_ui()
        if self.state.execution_id is not None:
            self.cancel_worker = self.run_worker(
                self.cancel(self.state.execution_id),
                name="pleiad-cancel",
                exclusive=False,
            )

    async def cancel(self, execution_id: str) -> None:
        """Cancel one execution and apply the returned durable record."""
        record = await self.service.cancel(execution_id)
        if record is not None:
            self.state.apply_execution_record(record)
            self.sync_ui()
            self.focus_composer()

    def action_clear_input(self) -> None:
        """Clear only the current Composer draft."""
        self.main_view.query_one(Composer).clear_input()

    def action_clear_screen(self) -> None:
        """Clear visible conversation items without deleting durable history."""
        self.state.clear_visible_conversation()
        self.sync_ui()

    def action_show_graph(self) -> None:
        """Select the Graph Inspector view unless the Composer owns the key."""
        self.show_inspector_view("graph")

    def action_show_runtime(self) -> None:
        """Select the Runtime Inspector view unless the Composer owns the key."""
        self.show_inspector_view("runtime")

    def action_open_trace(self) -> None:
        """Select the Agent Loop Trace Inspector view."""
        self.show_inspector_view("trace")

    def action_open_help(self) -> None:
        """Open help when the Composer is not editing ordinary text."""
        if isinstance(self.focused, Input):
            return
        self.push_screen(HelpScreen())

    def show_inspector_view(self, view: str) -> None:
        """Switch an Inspector view only when a normal Composer draft is not focused."""
        if isinstance(self.focused, Input):
            return
        self.main_view.show_view(view)

    def handle_command(self, value: str) -> None:
        """Handle slash commands and colon view commands."""
        self.main_view.query_one(Composer).clear_input()
        command = value.lower()
        if command == "/help":
            self.push_screen(HelpScreen())
        elif command == "/approvals":
            self.run_worker(self.show_approvals(), name="pleiad-approvals", group="approvals", exclusive=True)
        elif command == ":graph":
            self.main_view.show_view("graph")
        elif command == ":runtime":
            self.main_view.show_view("runtime")
        elif command == ":trace":
            self.main_view.show_view("trace")
        elif command == ":clear":
            self.action_clear_screen()
        elif command == ":switch model":
            self.notify(
                "Model selection is managed by global configuration; runtime switching is unavailable.",
                severity="information",
            )
        elif command == ":export log":
            self.notify("Program Log export is not available in the TUI yet.", severity="information")
        else:
            self.notify(f"Unknown command: {value}", severity="warning")

    async def show_approvals(self, execution_id: str | None = None, *, notify_empty: bool = True) -> None:
        """Load persisted requests and open their explicit decision screen."""
        if isinstance(self.screen, ApprovalScreen):
            await self.refresh_approvals()
            return
        pending = await self.service.approvals(execution_id)
        if not pending:
            if notify_empty:
                self.notify("No pending approval requests.")
            return
        self.push_screen(ApprovalScreen(pending, execution_id), self.approval_selected)

    async def refresh_approvals(self) -> None:
        """Remove settled actions without replacing the user's surviving selection."""
        screen = self.screen
        if not isinstance(screen, ApprovalScreen):
            return
        pending = await self.service.approvals(screen.execution_id)
        current = self.screen
        if not isinstance(current, ApprovalScreen) or current is not screen:
            return
        if not pending:
            await current.dismiss(None)
            self.focus_composer()
        elif pending != current.approvals:
            current.update_approvals(pending)

    def approval_selected(self, selection: ApprovalSelection | None) -> None:
        """Schedule exactly the decision returned by the user's selection."""
        if selection is None:
            return
        request, decision = selection
        self.state.execution_id = request.execution_id
        self.state.durable_record_applied = False
        self.state.input_status = InputStatus.SUBMITTING
        self.sync_ui()
        self.run_worker(
            self.resolve_approval(request.execution_id, request.interruption.interruption_id, decision),
            name="pleiad-approval-decision",
            group="approval-decision",
            exclusive=True,
        )

    async def resolve_approval(
        self, execution_id: str, interruption_id: str, decision: Literal["approve", "reject"]
    ) -> None:
        """Submit a real service decision and display its next durable state."""
        try:
            queued = await self.service.decide(execution_id, interruption_id, decision)
            self.state.apply_execution_record(queued)
            self.sync_ui()
            record = await self.service.wait(execution_id)
            if record is None:
                raise LookupError(f"Execution disappeared after its approval decision: {execution_id}")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logging.getLogger(__name__).exception(f"Approval decision failed execution_id={execution_id}")
            current = await asyncio.to_thread(self.service.get, execution_id)
            if current is None:
                self.state.input_status = InputStatus.ERROR
            else:
                self.state.apply_execution_record(current)
            self.sync_ui()
            self.notify(f"Approval decision failed: {error}", severity="error")
            return
        self.state.apply_execution_record(record)
        self.sync_ui()
        self.focus_composer()
        if record.status is ExecutionStatus.WAITING_APPROVAL:
            await self.show_approvals(execution_id)

    def sync_ui(self) -> None:
        """Push the presentation state into the composed MainScreen."""
        self.main_view.update_state(self.state)

    def focus_composer(self) -> None:
        """Return focus to the objective input after an execution settles."""
        if not isinstance(self.screen, ApprovalScreen):
            self.main_view.query_one("#composer-input", Input).focus()

    def install_program_log_handler(self) -> None:
        """Attach one bounded UI handler to the two application logger roots."""
        self.program_log_loggers = tuple(logging.getLogger(name) for name in ("pleiad", "pleiad_weave"))
        for logger in self.program_log_loggers:
            logger.addHandler(self.program_log_handler)

    def remove_program_log_handler(self) -> None:
        """Detach the UI handler before service shutdown closes logging sinks."""
        for logger in self.program_log_loggers:
            logger.removeHandler(self.program_log_handler)
        self.program_log_loggers = ()


def model_context_from_service(service: ExecutionService) -> ModelContextView:
    """Read configured model metadata available at the current Weave boundary."""
    return ModelContextView(model_name=service.model_name, context_window_tokens=128_000)
