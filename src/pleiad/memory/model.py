"""Durable records owned by the main Agent Memory boundary."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from pleiad.contracts import ContractModel, JsonObject
from pleiad.llm.contracts import Message, MessageRole


class ConversationRole(StrEnum):
    """Roles visible in the product conversation."""

    USER = "user"
    ASSISTANT = "assistant"


class MemoryScope(StrEnum):
    """Ownership scopes supported by long-term Memory."""

    CONVERSATION = "conversation"
    USER = "user"


class LongTermMemoryKind(StrEnum):
    """Semantic kinds of durable long-term records."""

    SUMMARY = "summary"
    FACT = "fact"


class MemoryStatus(StrEnum):
    """Lifecycle state represented by the latest journal revision."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    """Runtime-owned Memory behavior and resource limits."""

    user_id: str = "default-user"
    cross_conversation_memory: bool = False
    profile_max_items: int = 20
    profile_max_tokens: int = 2_000
    recall_max_calls_per_execution: int = 3
    recall_max_results: int = 5
    recall_max_output_tokens: int = 2_000
    search_backend: str = "lexical"


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A retrieved Memory item with search-time provenance and ranking data."""

    memory_id: str
    scope: MemoryScope
    content: str
    kind: LongTermMemoryKind
    source_message_ids: tuple[str, ...]
    score: float | None
    updated_at: datetime
    metadata: JsonObject


class ConversationMessage(ContractModel):
    """One raw user-visible Conversation message.

    Only the original User Query or the final visible Assistant response may
    be stored here. Request-local business input and assembled Prompts are
    intentionally outside the durable Conversation source.
    """

    message_id: str = Field(
        min_length=1,
        description="Stable identifier used by long-term provenance links.",
    )
    conversation_id: str = Field(
        min_length=1,
        description="Conversation namespace shared by multiple executions.",
    )
    execution_id: str = Field(
        min_length=1,
        description="Top-level execution that produced this visible message.",
    )
    role: ConversationRole = Field(
        description="External role; internal System and Tool roles are excluded.",
    )
    content: str = Field(
        description="Original User Query or final visible Assistant content.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Creation time of the source message.",
    )


class MainAgentMessage(ContractModel):
    """One durable message in the main Agent short-term model session."""

    message_id: str = Field(
        min_length=1,
        description="Storage identity of this model message.",
    )
    conversation_id: str = Field(
        min_length=1,
        description="Conversation namespace of the main Agent session.",
    )
    execution_id: str = Field(
        min_length=1,
        description="Top-level user execution containing this message.",
    )
    turn_id: str = Field(
        min_length=1,
        description="Stable identifier of the main Agent turn that produced it.",
    )
    run_id: str | None = Field(
        default=None,
        description="Agent Loop identity; absent for the raw turn-opening User message.",
    )
    sequence: int = Field(
        ge=1,
        description="Monotonic order within one Conversation session.",
    )
    message: Message = Field(
        description="Provider-neutral User, Assistant, or Tool message payload.",
    )
    source_conversation_message_id: str | None = Field(
        default=None,
        description="L0 source identifier for the persisted raw User message.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Time at which the model message entered the journal.",
    )

    @model_validator(mode="after")
    def reject_system_prompt(self) -> Self:
        """Prevent request-local System Prompts from entering short-term Memory."""
        if self.message.role is MessageRole.SYSTEM:
            raise ValueError("system prompts are request-local and cannot enter memory")
        return self


class LongTermMemory(ContractModel):
    """One append-only, source-linked long-term Memory revision.

    Conversation-scoped records implement L1. User-scoped records implement
    L2. The scope owner is explicit so a future real user identity can replace
    the current default-user without changing the persistence contract.
    """

    memory_id: str = Field(
        min_length=1,
        description="Logical identity shared by successive revisions.",
    )
    scope: MemoryScope = Field(description="Ownership scope of this record.")
    scope_id: str = Field(
        min_length=1,
        description="Conversation ID or user ID owning this record.",
    )
    conversation_id: str | None = Field(
        default=None,
        description="Conversation provenance; optional for user-level records.",
    )
    kind: LongTermMemoryKind = Field(
        description="Conversation summary or extracted stable fact.",
    )
    content: str = Field(
        min_length=1,
        description="Human-readable long-term Memory payload.",
    )
    source_message_ids: tuple[str, ...] = Field(
        default=(),
        description="L0 message IDs supporting this derived record.",
    )
    status: MemoryStatus = Field(
        default=MemoryStatus.ACTIVE,
        description="Whether this revision participates in recall.",
    )
    revision: int = Field(
        default=1,
        ge=1,
        description="Revision number within one logical Memory ID.",
    )
    metadata: JsonObject = Field(
        default_factory=dict,
        description="Non-authoritative indexing and generator metadata.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Creation time of this immutable revision.",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Last update time represented by this immutable revision.",
    )

    @model_validator(mode="after")
    def validate_scope_provenance(self) -> Self:
        """Require Conversation provenance for Conversation-owned records."""
        if self.scope is MemoryScope.CONVERSATION and self.conversation_id != self.scope_id:
            raise ValueError("conversation Memory must use its conversation_id as scope_id")
        return self
