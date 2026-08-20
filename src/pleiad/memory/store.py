"""Ports and runtime handles for persistent main-Agent Memory."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pleiad.llm.contracts import Message
from pleiad.memory.model import (
    ConversationMessage,
    LongTermMemory,
    MemoryCandidate,
    MemoryPolicy,
)

ModelMessageSink = Callable[[str, Message], None]
CheckpointSink = Callable[[object], None]


class MainAgentMemory(Protocol):
    """Persistent Memory boundary owned by the main Pleiad Agent."""

    def begin_turn(
        self,
        conversation_id: str,
        execution_id: str,
        raw_query: str,
    ) -> "MainAgentTurn":
        """Persist a raw User turn and restore short-term context."""

    def recall_memory(
        self,
        *,
        conversation_id: str,
        query: str,
        limit: int,
        policy: MemoryPolicy,
    ) -> tuple[MemoryCandidate, ...]:
        """Search permitted long-term Memory for the main Agent."""

    def finish_turn(
        self,
        turn: "MainAgentTurn",
        assistant_content: str,
        *,
        succeeded: bool,
    ) -> ConversationMessage | None:
        """Persist the visible Assistant result."""

    def append_long_term(self, memory: LongTermMemory) -> None:
        """Append one source-linked summary or fact revision."""

    def append_checkpoint(self, checkpoint: object) -> None:
        """Append one request-context compaction checkpoint."""


@dataclass(frozen=True, slots=True)
class MainAgentTurn:
    """One main-Agent turn with request-local profile and message context."""

    conversation_id: str
    execution_id: str
    user_message: ConversationMessage
    history: tuple[Message, ...]
    personal_profile: tuple[MemoryCandidate, ...]
    append_model_message: ModelMessageSink
    history_sequence: int
    checkpoint_handoff: str | None
    append_checkpoint: CheckpointSink

    @property
    def turn_id(self) -> str:
        """Return the stable turn identifier used by the session journal."""
        return self.execution_id
