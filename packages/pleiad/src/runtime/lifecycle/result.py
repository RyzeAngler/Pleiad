from dataclasses import dataclass, field

from pleiad.contracts import JsonObject
from pleiad.errors.base import BusinessError, PleiadError
from pleiad.runtime.lifecycle.state import RunState, RunStatus, RunUsage
from pleiad.tools.interruption import ToolInterruption


@dataclass(frozen=True, slots=True)
class RunResult:
    """Stable public result of one Agent Run."""

    run_id: str
    status: RunStatus
    final_output: str = ""
    structured_output: JsonObject | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    checkpoint: RunState | None = None
    interruptions: tuple[ToolInterruption, ...] = ()
    error: PleiadError | None = None

    def __post_init__(self) -> None:
        """Validate status-specific result invariants."""
        if self.status is RunStatus.SUCCEEDED:
            if self.checkpoint is not None or self.interruptions or self.error is not None:
                raise ValueError("a successful Run cannot contain control state or an error")
        elif self.status is RunStatus.FAILED:
            if not isinstance(self.error, BusinessError):
                raise ValueError("a failed Run requires a business error")
            if self.checkpoint is not None or self.interruptions:
                raise ValueError("a failed Run cannot contain a checkpoint or interruptions")
        elif self.status is RunStatus.INTERRUPTED:
            if self.checkpoint is None or not self.interruptions:
                raise ValueError("an interrupted Run requires a checkpoint and interruption")
            if self.checkpoint.interruptions != self.interruptions or self.error is not None:
                raise ValueError("interrupted Run state must match its checkpoint")
        elif self.error is not None or self.interruptions:
            raise ValueError("control results cannot contain an error or interruptions")
