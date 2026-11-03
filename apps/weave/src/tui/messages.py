from __future__ import annotations

from typing import TYPE_CHECKING

from textual.message import Message

from pleiad_weave.tui.state import LogRecordView

if TYPE_CHECKING:
    from pleiad_weave.application.events import ExecutionEvent


class ExecutionEventMessage(Message):
    """Thread-safe wrapper for one Runtime Event."""

    def __init__(self, event: ExecutionEvent) -> None:
        super().__init__()
        self.event = event


class ProgramLogMessage(Message):
    """Thread-safe wrapper for one formatted Program Log record."""

    def __init__(self, record: LogRecordView) -> None:
        super().__init__()
        self.record = record
