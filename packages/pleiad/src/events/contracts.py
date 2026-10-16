from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field

from pleiad.contracts import ContractModel


class Event(ContractModel):
    """Stable SDK event envelope that applications may extend by inheritance."""

    event_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    kind: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: Literal[1] = 1
    run_id: str | None = Field(default=None, min_length=1)


EventSink = Callable[[Event], Awaitable[None]]
