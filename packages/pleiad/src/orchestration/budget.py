from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from pydantic import Field, model_validator

from pleiad.contracts import ContractModel
from pleiad.orchestration.errors import OrchestrationStateError
from pleiad.runtime.lifecycle.state import RunUsage
from pleiad.runtime.policy import EffectivePolicy
from pleiad.tools.contracts import ParentRunLimits, ToolContext


class BudgetState(ContractModel):
    """Persist settled child allowances separately from a parent's own usage."""

    remaining_turns: int
    remaining_output_tokens: int
    parent_remaining_turns: int = Field(ge=0)
    parent_remaining_output_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_allowance(self) -> "BudgetState":
        """Reject snapshots that grant more than the observed parent allowance."""
        if (
            self.remaining_turns > self.parent_remaining_turns
            or self.remaining_output_tokens > self.parent_remaining_output_tokens
        ):
            raise ValueError("Settled child allowance cannot exceed the parent allowance")
        return self


@dataclass(slots=True)
class ParentBudget:
    """Keep child reservations and newly observed parent charges in one pool."""

    remaining_turns: int
    remaining_output_tokens: int
    parent_remaining_turns: int
    parent_remaining_output_tokens: int


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """Hold an exclusive allowance until a child supplies its confirmed usage."""

    parent_run_id: str
    turns: int
    output_tokens: int


class BudgetLedger:
    """Reserve and settle child allowances for Graph and Supervisor owners."""

    def __init__(self) -> None:
        """Create empty pools scoped by the invoking parent Run identity."""
        self.pools: dict[str, ParentBudget] = {}
        self.reservations: dict[str, BudgetReservation] = {}

    def reserve(
        self,
        reservation_id: str,
        parent_run_id: str,
        limits: ParentRunLimits,
        max_turns: int,
        max_output_tokens: int,
    ) -> BudgetReservation | None:
        """Take disjoint allowance, or return None until outstanding work settles."""
        if reservation_id in self.reservations:
            raise OrchestrationStateError("A child budget reservation already exists.")
        pool = self.pools.get(parent_run_id)
        if pool is None:
            pool = ParentBudget(
                limits.remaining_turns,
                limits.remaining_output_tokens,
                limits.remaining_turns,
                limits.remaining_output_tokens,
            )
            self.pools[parent_run_id] = pool
        else:
            parent_turns = min(pool.parent_remaining_turns, limits.remaining_turns)
            parent_tokens = min(pool.parent_remaining_output_tokens, limits.remaining_output_tokens)
            # A deficit must survive child refunds; otherwise parent charges would be restored.
            pool.remaining_turns -= pool.parent_remaining_turns - parent_turns
            pool.remaining_output_tokens -= pool.parent_remaining_output_tokens - parent_tokens
            pool.parent_remaining_turns = parent_turns
            pool.parent_remaining_output_tokens = parent_tokens
        turns = min(max_turns, pool.remaining_turns)
        output_tokens = min(max_output_tokens, pool.remaining_output_tokens)
        if turns < 1 or output_tokens < 1:
            return None
        pool.remaining_turns -= turns
        pool.remaining_output_tokens -= output_tokens
        reservation = BudgetReservation(parent_run_id, turns, output_tokens)
        self.reservations[reservation_id] = reservation
        return reservation

    def settle(self, reservation_id: str, usage: RunUsage | None, *, interrupted: bool = False) -> None:
        """Refund confirmed unused allowance; an unknown outcome charges the full reservation."""
        if interrupted:
            return
        reservation = self.reservations.pop(reservation_id, None)
        if reservation is None or usage is None:
            return
        pool = self.pools[reservation.parent_run_id]
        pool.remaining_turns += reservation.turns - usage.model_calls
        pool.remaining_output_tokens += reservation.output_tokens - usage.budget_output_tokens

    def snapshot(self, parent_run_id: str) -> BudgetState | None:
        """Return settled allowances that can be restored without live child tasks."""
        if any(item.parent_run_id == parent_run_id for item in self.reservations.values()):
            raise OrchestrationStateError("Active child reservations cannot be saved as settled budget state.")
        pool = self.pools.get(parent_run_id)
        return None if pool is None else BudgetState.model_validate(asdict(pool))

    def restore(self, parent_run_id: str, state: BudgetState) -> None:
        """Restore one settled pool without replenishing an already live budget."""
        existing = self.snapshot(parent_run_id)
        if existing is not None and existing != state:
            raise OrchestrationStateError("Restored child budget differs from the live parent pool.")
        self.pools[parent_run_id] = ParentBudget(
            state.remaining_turns,
            state.remaining_output_tokens,
            state.parent_remaining_turns,
            state.parent_remaining_output_tokens,
        )

    def release(self, parent_run_id: str) -> None:
        """Release a terminal parent's pool only after all child reservations settle."""
        self.snapshot(parent_run_id)
        self.pools.pop(parent_run_id, None)


def child_policy(context: ToolContext) -> EffectivePolicy | None:
    """Intersect nested execution with the parent deadline, grants, and remaining allowance."""
    limits = context.parent_limits
    if limits is None:
        return None
    return EffectivePolicy(
        timeout_seconds=max(0.0, (limits.deadline - datetime.now(UTC)).total_seconds()),
        max_turns=limits.remaining_turns,
        max_output_tokens=limits.remaining_output_tokens,
        tool_concurrency=limits.tool_concurrency,
        grants=context.grants,
        deadline=limits.deadline,
    )
