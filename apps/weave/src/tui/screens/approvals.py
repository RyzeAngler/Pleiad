import json
from typing import Literal

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from pleiad_weave.application.dto import ApprovalRequest

type ApprovalSelection = tuple[ApprovalRequest, Literal["approve", "reject"]]


class ApprovalScreen(ModalScreen[ApprovalSelection | None]):
    """Show exact pending actions and return the user's explicit decision."""

    BINDINGS = [("escape", "close", "Close"), ("a", "approve", "Approve"), ("r", "reject", "Reject")]  # noqa: RUF012

    def __init__(self, approvals: tuple[ApprovalRequest, ...], execution_id: str | None = None) -> None:
        """Bind the persisted requests without selecting a decision."""
        super().__init__()
        self.approvals = approvals
        self.execution_id = execution_id

    def compose(self) -> ComposeResult:
        """Build a keyboard-accessible request list and full argument view."""
        with Vertical(id="approval-dialog"):
            yield Static("TOOL APPROVAL · Select a request, inspect its arguments, then decide", id="approval-title")
            yield DataTable(id="approval-list", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="approval-body"):
                yield Static(id="approval-details", markup=False)
            with Horizontal(id="approval-actions"):
                yield Button("Approve [a]", id="approval-approve", variant="success", disabled=not self.approvals)
                yield Button("Reject [r]", id="approval-reject", variant="error", disabled=not self.approvals)
                yield Button("Close [Esc]", id="approval-close")

    def on_mount(self) -> None:
        """Populate persisted identities and focus selection instead of approval."""
        table = self.query_one("#approval-list", DataTable)
        table.add_columns("Execution", "Tool", "Deadline")
        self.update_approvals(self.approvals)
        table.focus()

    def update_approvals(self, approvals: tuple[ApprovalRequest, ...]) -> None:
        """Refresh pending rows while retaining the selected action and focus.

        Args:
            approvals: Current persisted pending actions within this screen's scope.
        """
        table = self.query_one("#approval-list", DataTable)
        selected = self.selected_request()
        previous_row = table.cursor_row
        self.approvals = approvals
        selected_row = next(
            (
                index
                for index, request in enumerate(approvals)
                if selected is not None
                and request.execution_id == selected.execution_id
                and request.interruption.interruption_id == selected.interruption.interruption_id
            ),
            None,
        )
        table.clear()
        for index, request in enumerate(approvals):
            table.add_row(
                request.execution_id,
                request.interruption.tool_name,
                request.deadline.astimezone().isoformat(timespec="seconds"),
                key=str(index),
            )
        if approvals:
            row = min(previous_row, len(approvals) - 1) if selected_row is None else selected_row
            table.move_cursor(row=row, scroll=False)
        self.query_one("#approval-approve", Button).disabled = not approvals
        self.query_one("#approval-reject", Button).disabled = not approvals
        self.update_details()
        if selected is not None and selected_row is None:
            table.focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Update the exact action preview when keyboard or mouse selection changes."""
        self.update_details()

    def update_details(self) -> None:
        """Render all action arguments as plain text without Rich markup."""
        selected = self.selected_request()
        if selected is None:
            content = "No pending approval requests."
        else:
            interruption = selected.interruption
            arguments = json.dumps(interruption.arguments, ensure_ascii=False, indent=2, sort_keys=True)
            resolved = {key: value for key, value in interruption.details.items() if key != "kind"}
            target = ""
            if resolved:
                target = "\n\nResolved action:\n" + json.dumps(resolved, ensure_ascii=False, indent=2, sort_keys=True)
            content = (
                f"Execution: {selected.execution_id}\n"
                f"Request: {interruption.interruption_id}\n"
                f"Tool: {interruption.tool_name} ({interruption.tool_code})\n"
                f"Reason: {interruption.reason}\n"
                f"Deadline: {selected.deadline.astimezone().isoformat(timespec='seconds')}\n\n"
                f"Arguments:\n{arguments}{target}"
            )
        self.query_one("#approval-details", Static).update(content)

    def selected_request(self) -> ApprovalRequest | None:
        """Return the selected persisted request, if the list is non-empty."""
        row = self.query_one("#approval-list", DataTable).cursor_row
        return self.approvals[row] if 0 <= row < len(self.approvals) else None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route visible action buttons to the same keyboard decision paths."""
        if event.button.id == "approval-approve":
            self.action_approve()
        elif event.button.id == "approval-reject":
            self.action_reject()
        elif event.button.id == "approval-close":
            self.action_close()

    def action_approve(self) -> None:
        """Return an explicit approval for the currently inspected request."""
        selected = self.selected_request()
        if selected is not None:
            self.dismiss((selected, "approve"))

    def action_reject(self) -> None:
        """Return an explicit rejection for the currently inspected request."""
        selected = self.selected_request()
        if selected is not None:
            self.dismiss((selected, "reject"))

    def action_close(self) -> None:
        """Leave all requests pending and return to the conversation."""
        self.dismiss(None)
