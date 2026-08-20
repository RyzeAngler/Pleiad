"""Immutable request-local Context contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import Field

from pleiad.contracts import ContractModel, JsonObject

if TYPE_CHECKING:
    from pleiad.memory.model import MemoryCandidate


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """One selected dynamic context item with source provenance."""

    source_type: str
    source_id: str
    content: str
    score: float | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """Structured dynamic context selected for one model request."""

    blocks: tuple[ContextBlock, ...] = ()
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContextRequest:
    """Inputs available while constructing one request-local Context."""

    agent_id: str
    conversation_id: str
    execution_id: str
    raw_query: str
    business_input: JsonObject
    personal_profile: tuple[MemoryCandidate, ...] = ()
    checkpoint_handoff: str | None = None
    runtime_state: JsonObject = field(default_factory=dict)


class ContextCheckpoint(ContractModel):
    """Durable handoff for a compacted request-local model history."""

    checkpoint_id: str
    conversation_id: str
    execution_id: str
    covered_until_sequence: int
    handoff: str
    task_state_version: str | None = None
    artifact_refs: tuple[str, ...] = ()
    pending_operations: tuple[str, ...] = ()
    input_tokens_before: int = 0
    input_tokens_after: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
