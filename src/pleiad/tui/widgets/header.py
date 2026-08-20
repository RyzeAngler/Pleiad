"""Top-level Pleiad status bar."""

from rich.text import Text
from textual.widgets import Static

from pleiad.tui.state import TuiExecutionStatus, TuiState


class HeaderBar(Static):
    """Render global model, Execution, and Graph status."""

    def update_state(self, state: TuiState) -> None:
        """Update the header without reading Runtime internals."""
        status, style = _status_label(state.execution_status)
        revision = state.graph_revision or "-"
        text = Text()
        text.append("✦ Pleiad", style="bold #66C8F2")
        text.append("  |  ", style="#526477")
        text.append(state.model_label, style="#9AA9BB")
        text.append("  |  ", style="#526477")
        text.append(f"● {status}", style=style)
        text.append("  |  ", style="#526477")
        text.append(f"rev: {revision}", style="#9AA9BB")
        if state.execution_id:
            text.append(f"  |  exec: {state.execution_id[:12]}", style="#788899")
        text.append("\n  Pleiades (M45)", style="#B59AF4")
        text.append("   RA 03h 47m   Dec +24° 07′", style="#788899")
        self.update(text)


def _status_label(status: TuiExecutionStatus) -> tuple[str, str]:
    if status is TuiExecutionStatus.RUNNING:
        return "Running", "bold #F0B84B"
    if status is TuiExecutionStatus.SUCCEEDED:
        return "Completed", "bold #78D17B"
    if status is TuiExecutionStatus.FAILED:
        return "Failed", "bold #F06C75"
    if status is TuiExecutionStatus.CANCELLED:
        return "Canceled", "bold #F0B84B"
    return "Idle", "bold #9AA9BB"
