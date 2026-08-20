"""Conversation and compact Tool activity rendering."""

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from pleiad.tui.state import MessageView, TuiState
from pleiad.tui.widgets.process import ProcessPane


class ConversationPane(Vertical):
    """Conversation header plus a vertically scrollable Rich message log."""

    def compose(self) -> ComposeResult:
        yield Static(
            "Conversation",
            id="conversation-header",
        )
        yield RichLog(
            id="conversation-log",
            markup=False,
            wrap=True,
            highlight=False,
            auto_scroll=True,
        )
        yield ProcessPane(id="process-pane")

    def update_state(self, state: TuiState) -> None:
        """Rebuild only the conversation projection."""
        log = self.query_one("#conversation-log", RichLog)
        follow_tail = log.scroll_y >= log.max_scroll_y - 1
        log.clear()
        if not state.messages:
            log.write(
                Text(
                    "Pleiad is ready.\n\nDescribe a goal for Pleiad.\n"
                    "Type /help to see available commands.",
                    style="#9AA9BB",
                ),
                scroll_end=True,
            )
            self.query_one(ProcessPane).update_state(state)
            return
        for index, message in enumerate(state.messages):
            if index and message.role == "user":
                log.write(Text(""), scroll_end=follow_tail)
            log.write(_render_message(message), scroll_end=follow_tail)
        self.query_one(ProcessPane).update_state(state)


def _render_message(message: MessageView) -> Group:
    role_style = "#66C8F2" if message.role == "user" else "#B59AF4"
    role = "User:" if message.role == "user" else "Pleiad:"
    heading = Text(role, style=f"bold {role_style}")
    body = message.content or "Working..."
    body_renderable = (
        Markdown(body)
        if message.role == "pleiad" and message.content
        else Text(body, style="#D8E1EC" if message.role == "user" else "#9AA9BB")
    )
    return Group(
        heading,
        body_renderable,
    )
