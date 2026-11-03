from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Input, Static

from pleiad_weave.tui.state import InputStatus


class Composer(Horizontal):
    """Single-line objective input with a compact state indicator."""

    class Submitted(Message):
        """Posted when the user presses Enter with non-empty input."""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def compose(self) -> ComposeResult:
        yield Static(">", id="composer-label")
        yield Input(placeholder="Describe the next objective…", id="composer-input")
        yield Static(id="composer-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Forward the input without clearing it before the application accepts it."""
        value = event.value.strip()
        if value:
            self.post_message(self.Submitted(value))

    def clear_input(self) -> None:
        """Clear the draft after the application has accepted a submission."""
        self.query_one("#composer-input", Input).value = ""

    def set_status(self, status: InputStatus) -> None:
        """Render the current input lifecycle state."""
        labels = {
            InputStatus.IDLE: "READY",
            InputStatus.SUBMITTING: "SUBMITTING",
            InputStatus.BLOCKED: "EXECUTING",
            InputStatus.APPROVAL: "APPROVAL",
            InputStatus.ERROR: "ERROR",
        }
        hint = self.query_one("#composer-hint", Static)
        label = labels[status]
        style = "#56D364" if status is InputStatus.IDLE else "#EAB308"
        if status is InputStatus.ERROR:
            style = "#EF6B73"
        hint.update(f"{label}")
        hint.styles.color = style
