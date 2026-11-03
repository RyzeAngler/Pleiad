from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Markdown, Static


class HelpScreen(Screen[None]):
    """Display complete keyboard and command help without changing the main view."""

    BINDINGS = [("escape", "app.pop_screen", "Close")]  # noqa: RUF012

    def compose(self) -> ComposeResult:
        yield Static("WEAVE · HELP  ·  Press Esc to close", id="help-title")
        yield VerticalScroll(
            Markdown(
                """
                ## Input

                - Enter: submit the Composer objective
                - Esc: cancel the active execution
                - Ctrl+C: cancel while running, exit while idle
                - Ctrl+K: clear the current draft
                - Ctrl+L: clear visible conversation items

                ## Inspector views

                - `r`: Runtime and Program Log
                - `g`: Graph nodes and edges
                - `t`: Agent Loop Trace events
                - On narrow terminals, `CHAT / RUNTIME / GRAPH / TRACE` tabs select the view

                ## Conversation

                - End: follow the newest conversation or Program Log content
                - Enter or Space on a collapsed Process Group: expand safe process summaries
                - Scroll up: pause auto-follow in that scroll region

                ## Commands

                - `?` or `/help`: open this page
                - `/approvals`: inspect pending Tool arguments and approve or reject
                - `:runtime`, `:graph`, `:trace`: select an Inspector view
                - `:clear`: clear visible conversation items
                - `:switch model`: show model configuration status
                """
            ),
            id="help-body",
        )
