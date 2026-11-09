from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from pleiad.contracts import JsonObject
from pleiad.events.contracts import Event


class DelegationStatus(StrEnum):
    """Lifecycle states owned by one SubagentGroup delegation."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class SubagentEvent(Event):
    """Correlate one Subagent lifecycle fact with its parent and child Runs."""

    delegation_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    parent_run_id: str | None = Field(default=None, min_length=1)
    child_run_id: str | None = Field(default=None, min_length=1)
    subagent_id: str = Field(min_length=1)


class SubagentSubmittedEvent(SubagentEvent):
    """Report acceptance of one Subagent delegation."""

    kind: Literal["subagent.submitted"] = "subagent.submitted"


class SubagentStartedEvent(SubagentEvent):
    """Report that one accepted Subagent has begun execution."""

    kind: Literal["subagent.started"] = "subagent.started"


class SubagentCompletedEvent(SubagentEvent):
    """Carry one persisted terminal Subagent result."""

    kind: Literal["subagent.completed"] = "subagent.completed"
    status: DelegationStatus
    result: JsonObject | None = None
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)


class SubagentConsumedEvent(SubagentEvent):
    """Report consumption of one completed Subagent result."""

    kind: Literal["subagent.consumed"] = "subagent.consumed"
    completion_event_id: str = Field(min_length=1)


SubagentLifecycleEvent = Annotated[
    SubagentSubmittedEvent | SubagentStartedEvent | SubagentCompletedEvent | SubagentConsumedEvent,
    Field(discriminator="kind"),
]
