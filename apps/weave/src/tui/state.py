from dataclasses import dataclass, field
from typing import Literal

from pleiad.contracts import JsonObject

from pleiad_weave.infrastructure.storage.product_models import (
    AttentionView,
    ChatMessageRequest,
    Execution,
    Message,
    ResolutionRequest,
    Snapshot,
)


@dataclass(slots=True)
class TuiState:
    """Keep interface drafts apart from committed shared product facts."""

    snapshot: Snapshot | None = None
    messages: dict[str, Message] = field(default_factory=dict)
    category: Literal["executions", "attentions", "spaces"] = "executions"
    selected_id: str | None = None
    detail: JsonObject | None = None
    execution: Execution | None = None
    attention: AttentionView | None = None
    answer_to: AttentionView | None = None
    target_execution_ids: tuple[str, ...] = ()
    pending_message: ChatMessageRequest | None = None
    pending_resolution: ResolutionRequest | None = None
    pending_resolution_target: str | None = None
    read_sequence: int = 0
    cache_generation: int = 0
    selection_version: int = 0
    detail_stale: bool = True
    connected: bool = False
    submitting: bool = False

    def apply_snapshot(self, snapshot: Snapshot) -> None:
        """Refresh owner facts while preserving the composer and inspected identity."""
        self.snapshot = snapshot
        self.messages.update((message.message_id, message) for message in snapshot.messages.items)
        self.read_sequence = snapshot.snapshot_sequence
        self.connected = True
