from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from pleiad_weave.tui.state import TuiState


class ModelContextFooter(Static):
    """Render the selected model and latest input-context usage."""

    def update_state(self, state: TuiState) -> None:
        """Update the footer without adding commands or execution status."""
        model = state.model_context.model_name or "unavailable"
        used = format_tokens(state.model_context.used_context_tokens)
        window = format_tokens(state.model_context.context_window_tokens)
        left = Text()
        left.append("MODEL  ", style="bold #5BD6E8")
        left.append(model, style="#E0E4E8")
        right = Text()
        right.append("CONTEXT  ", style="bold #5BD6E8")
        right.append(f"{used} / {window} tokens", style=context_color(state))
        table = Table.grid(expand=True, padding=(0, 0))
        table.add_column(ratio=1)
        table.add_column(justify="right")
        table.add_row(left, right)
        self.update(table)


def format_tokens(tokens: int | None) -> str:
    """Format an optional token count without inventing zero for unknown data."""
    return "—" if tokens is None else f"{tokens:,}"


def context_color(state: TuiState) -> str:
    """Return the context usage color for the current model request."""
    used = state.model_context.used_context_tokens
    window = state.model_context.context_window_tokens
    if used is None or window is None or window <= 0:
        return "#E0E4E8"
    ratio = used / window
    if ratio >= 0.9:
        return "#EF6B73"
    if ratio >= 0.7:
        return "#EAB308"
    return "#E0E4E8"
