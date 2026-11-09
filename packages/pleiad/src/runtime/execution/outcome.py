from dataclasses import dataclass, field
from enum import StrEnum

from pleiad.contracts import JsonObject
from pleiad.errors.base import PleiadError
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.usage import RunUsage
from pleiad.tools.interruption import ToolInterruption


class LoopStatus(StrEnum):
    """Describe why the active execution scope stopped advancing."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PAUSED = "paused"
    TIMED_OUT = "timeout"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """Carry execution facts to the Runner's durable commit boundary."""

    status: LoopStatus
    final_output: str = ""
    structured_output: JsonObject | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    checkpoint: RunCheckpoint | None = None
    interruptions: tuple[ToolInterruption, ...] = ()
    error: PleiadError | None = None
