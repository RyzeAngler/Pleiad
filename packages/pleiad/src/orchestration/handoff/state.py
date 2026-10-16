from enum import StrEnum

from pydantic import Field

from pleiad.contracts import ContractModel


class HandoffPhase(StrEnum):
    """Durable phases of one Runtime control transition."""

    PREPARED = "prepared"
    MEMORY_COMMITTED = "memory_committed"
    SWITCH_APPLIED = "switch_applied"


class HandoffRecord(ContractModel):
    """Serializable transition facts used for idempotent recovery."""

    transition_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    source_agent: str = Field(min_length=1)
    target_agent: str = Field(min_length=1)
    phase: HandoffPhase


class HandoffState(ContractModel):
    """Serializable Handoff history independent from ShortMemory messages."""

    records: tuple[HandoffRecord, ...] = ()
    count: int = Field(default=0, ge=0)
