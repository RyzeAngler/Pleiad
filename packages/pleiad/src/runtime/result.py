from dataclasses import dataclass

from pleiad.contracts import JsonObject
from pleiad.errors.base import PleiadError
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.records import RunReceipt, RunStatus
from pleiad.sessions.usage import RunUsage
from pleiad.tools.interruption import ToolInterruption


@dataclass(frozen=True, slots=True)
class RunResult:
    """Expose an already committed terminal receipt.

    Args:
        receipt: Durable execution facts committed before publication.
        error: Original process-local exception when available.
    """

    receipt: RunReceipt
    error: PleiadError | None = None

    @property
    def run_id(self) -> str:
        """Return the SDK invocation identity."""
        return self.receipt.run_id

    @property
    def state(self) -> RunStatus:
        """Return the committed terminal state."""
        return RunStatus(self.receipt.state)

    @property
    def reason(self) -> str | None:
        """Return the precise completion or failure reason."""
        return self.receipt.reason

    @property
    def final_output(self) -> str:
        """Return the accepted final text."""
        return self.receipt.final_output

    @property
    def structured_output(self) -> JsonObject | None:
        """Return validated structured output, when requested."""
        return self.receipt.structured_output

    @property
    def usage(self) -> RunUsage:
        """Return measured usage and conservative budget charges."""
        return self.receipt.usage


@dataclass(frozen=True, slots=True)
class RunSuspension:
    """Expose a safely persisted checkpoint after active resources settle."""

    checkpoint: RunCheckpoint

    @property
    def run_id(self) -> str:
        """Return the original SDK invocation identity."""
        return self.checkpoint.run_id

    @property
    def state(self) -> RunStatus:
        """Return the resumable paused state."""
        return RunStatus.PAUSED

    @property
    def usage(self) -> RunUsage:
        """Return cumulative usage at the safe boundary."""
        return self.checkpoint.usage

    @property
    def interruptions(self) -> tuple[ToolInterruption, ...]:
        """Return persisted requests requiring host decisions."""
        return self.checkpoint.interruptions


type RunOutcome = RunResult | RunSuspension
