"""User objective input widget."""

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Input, Static


class Composer(Horizontal):
    """Single-line objective input matching the first TUI design slice."""

    class Submitted(Message):
        """Posted when the user presses Enter."""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def compose(self) -> ComposeResult:
        yield Static("Pleiad>", id="composer-label")
        yield Input(placeholder="Type your message...", id="composer-input")
        yield Static("Enter to send", id="composer-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Forward a clean objective to the application."""
        value = event.value.strip()
        event.input.value = ""
        if value:
            self.post_message(self.Submitted(value))

    def set_status(self, status: str) -> None:
        """Update the hint without changing submission semantics."""
        hint = self.query_one("#composer-hint", Static)
        hint.update(
            {
                "idle": "Enter to send",
                "blocked": "Execution in progress · Press Esc to cancel",
                "submitting": "Submitting...",
                "error": "Input unavailable",
            }.get(status, status)
        )
