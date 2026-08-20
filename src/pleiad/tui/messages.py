"""Messages crossing from Runtime callbacks into the Textual event loop."""

from textual.message import Message

from pleiad.runtime.events import RuntimeEvent


class RuntimeEventMessage(Message):
    """Thread-safe wrapper for one Runtime Event."""

    def __init__(self, event: RuntimeEvent) -> None:
        super().__init__()
        self.event = event
