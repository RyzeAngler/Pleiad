"""Help screen for the first TUI command set."""

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Markdown, Static


class HelpScreen(Screen[None]):
    """Display keyboard commands without leaving the main application."""

    BINDINGS = [("escape", "app.pop_screen", "Close")]  # noqa: RUF012

    def compose(self) -> ComposeResult:
        yield Static("Pleiad Help  ·  Press Esc to close", id="help-title")
        yield VerticalScroll(
            Markdown(
                """
                ## Commands

                - Enter: submit a goal
                - Esc: cancel the active execution or close this page
                - Ctrl+C: exit while idle, request cancellation while running
                - Ctrl+K: clear the input draft
                - Ctrl+L: clear the visible conversation projection
                - `:show graph`: show the generated Graph
                - `:trace`: inspect the current execution Trace
                - `:clear`: clear the visible conversation
                """
            ),
            id="help-body",
        )
