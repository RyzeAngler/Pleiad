from dataclasses import asdict, dataclass, replace

from pydantic import Field, model_validator

from pleiad.contracts import ContractModel, UsageCharge
from pleiad.errors.base import SystemFault
from pleiad.tools.contracts import ParentRunLimits


class BudgetStateError(SystemFault):
    """Reject inconsistent reservation ownership or restored budget facts."""

    def __init__(self, message: str) -> None:
        super().__init__("budget.state_invalid", message)


class BudgetState(ContractModel):
    """Persist settled allowances and cumulative child charges for one parent."""

    remaining_model_steps: int
    remaining_output_tokens: int
    parent_remaining_model_steps: int = Field(ge=0)
    parent_remaining_output_tokens: int = Field(ge=0)
    usage_charges: tuple[UsageCharge, ...] = ()

    @model_validator(mode="after")
    def validate_allowance(self) -> "BudgetState":
        """Reject allowance expansion and duplicate cumulative child facts."""
        if (
            self.remaining_model_steps > self.parent_remaining_model_steps
            or self.remaining_output_tokens > self.parent_remaining_output_tokens
        ):
            raise ValueError("Settled child allowance cannot exceed the parent allowance")
        identities = tuple(value.charge_id for value in self.usage_charges)
        if len(identities) != len(set(identities)):
            raise ValueError("Child charge identities must be unique")
        return self


@dataclass(slots=True)
class ParentBudget:
    """Share actual parent model admission with exclusive child reservations."""

    remaining_model_steps: int
    remaining_output_tokens: int
    parent_remaining_model_steps: int
    parent_remaining_output_tokens: int
    usage_charges: tuple[UsageCharge, ...] = ()


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """Hold exclusive allowance until its original child settles."""

    parent_run_id: str
    model_steps: int
    output_tokens: int
    interrupted: bool = False


class ReservationState(ContractModel):
    """Persist one safely paused child's outstanding allowance."""

    reservation_id: str = Field(min_length=1)
    model_steps: int = Field(gt=0)
    output_tokens: int = Field(gt=0)


class BudgetCheckpoint(ContractModel):
    """Preserve a parent pool with all safely paused child reservations."""

    parent_run_id: str = Field(min_length=1)
    state: BudgetState
    reservations: tuple[ReservationState, ...] = ()

    @model_validator(mode="after")
    def validate_reservations(self) -> "BudgetCheckpoint":
        """Reject duplicate reservations and allowances outside the original pool."""
        identities = tuple(item.reservation_id for item in self.reservations)
        if len(identities) != len(set(identities)):
            raise ValueError("Budget checkpoint reservation IDs must be unique")
        if (
            self.state.remaining_model_steps + sum(item.model_steps for item in self.reservations)
            > self.state.parent_remaining_model_steps
            or self.state.remaining_output_tokens + sum(item.output_tokens for item in self.reservations)
            > self.state.parent_remaining_output_tokens
        ):
            raise ValueError("Budget checkpoint reservations cannot exceed the parent allowance")
        if any(value.parent_run_id != self.parent_run_id for value in self.state.usage_charges):
            raise ValueError("Child charges must belong to the checkpoint parent")
        return self


class BudgetLedger:
    """Own parent admission, child reservations, and idempotent cumulative charges."""

    def __init__(self) -> None:
        """Create isolated parent pools and original child reservation identities."""
        self.pools: dict[str, ParentBudget] = {}
        self.reservations: dict[str, BudgetReservation] = {}

    def observe_parent(self, parent_run_id: str, limits: ParentRunLimits) -> ParentBudget:
        """Debit only new parent-owned consumption, excluding already accounted children."""
        own_steps = limits.remaining_model_steps + limits.child_model_calls
        own_tokens = limits.remaining_output_tokens + limits.child_budget_output_tokens
        pool = self.pools.get(parent_run_id)
        if pool is None:
            pool = ParentBudget(own_steps, own_tokens, own_steps, own_tokens)
            self.pools[parent_run_id] = pool
        else:
            steps = min(pool.parent_remaining_model_steps, own_steps)
            tokens = min(pool.parent_remaining_output_tokens, own_tokens)
            pool.remaining_model_steps -= pool.parent_remaining_model_steps - steps
            pool.remaining_output_tokens -= pool.parent_remaining_output_tokens - tokens
            pool.parent_remaining_model_steps = steps
            pool.parent_remaining_output_tokens = tokens
        return pool

    def reserve(
        self,
        reservation_id: str,
        parent_run_id: str,
        limits: ParentRunLimits,
        max_model_steps: int,
        max_output_tokens: int,
    ) -> BudgetReservation | None:
        """Reserve disjoint allowance against the same pool used for parent models."""
        if reservation_id in self.reservations:
            raise BudgetStateError("A child budget reservation already exists.")
        pool = self.observe_parent(parent_run_id, limits)
        steps = min(max_model_steps, pool.remaining_model_steps)
        tokens = min(max_output_tokens, pool.remaining_output_tokens)
        if steps < 1 or tokens < 1:
            return None
        pool.remaining_model_steps -= steps
        pool.remaining_output_tokens -= tokens
        reservation = BudgetReservation(parent_run_id, steps, tokens)
        self.reservations[reservation_id] = reservation
        return reservation

    def usage_charges(self, parent_run_id: str) -> tuple[UsageCharge, ...]:
        """Read cumulative direct-child facts, including descendant cost within each child."""
        pool = self.pools.get(parent_run_id)
        return () if pool is None else pool.usage_charges

    def record_charge(self, value: UsageCharge) -> None:
        """Merge a monotonic charge once, respecting its held reservation."""
        pool = self.pools.get(value.parent_run_id)
        if pool is None:
            raise BudgetStateError("A child charge requires its admitted parent pool.")
        previous = next((item for item in pool.usage_charges if item.charge_id == value.charge_id), None)
        if previous is not None and (
            value.model_steps < previous.model_steps or value.output_tokens < previous.output_tokens
        ):
            raise BudgetStateError("Cumulative child usage cannot decrease.")
        reservation = self.reservations.get(value.charge_id)
        if reservation is not None:
            if reservation.parent_run_id != value.parent_run_id:
                raise BudgetStateError("Child usage belongs to another parent reservation.")
            pool.remaining_model_steps -= max(0, value.model_steps - reservation.model_steps) - max(
                0, (0 if previous is None else previous.model_steps) - reservation.model_steps
            )
            pool.remaining_output_tokens -= max(0, value.output_tokens - reservation.output_tokens) - max(
                0, (0 if previous is None else previous.output_tokens) - reservation.output_tokens
            )
        else:
            pool.remaining_model_steps -= value.model_steps - (0 if previous is None else previous.model_steps)
            pool.remaining_output_tokens -= value.output_tokens - (0 if previous is None else previous.output_tokens)
        pool.usage_charges = (*(item for item in pool.usage_charges if item.charge_id != value.charge_id), value)

    def settle(self, reservation_id: str, charge: UsageCharge | None, *, interrupted: bool = False) -> None:
        """Retain paused allowance and refund only confirmed unused terminal allowance."""
        reservation = self.reservations.get(reservation_id)
        if reservation is None:
            return
        value = (
            charge
            if charge is not None
            else UsageCharge(
                parent_run_id=reservation.parent_run_id,
                charge_id=reservation_id,
                model_steps=reservation.model_steps,
                output_tokens=reservation.output_tokens,
            )
        )
        if value.charge_id != reservation_id:
            raise BudgetStateError("Settlement must retain its original reservation identity.")
        self.record_charge(value)
        if interrupted:
            self.reservations[reservation_id] = replace(reservation, interrupted=True)
            return
        self.reservations.pop(reservation_id)
        pool = self.pools[reservation.parent_run_id]
        pool.remaining_model_steps += max(0, reservation.model_steps - value.model_steps)
        pool.remaining_output_tokens += max(0, reservation.output_tokens - value.output_tokens)

    def snapshot(self, parent_run_id: str) -> BudgetState | None:
        """Read settled facts when no child remains outstanding."""
        if any(value.parent_run_id == parent_run_id for value in self.reservations.values()):
            raise BudgetStateError("Active child reservations cannot be saved as settled budget state.")
        pool = self.pools.get(parent_run_id)
        return None if pool is None else BudgetState.model_validate(asdict(pool))

    def checkpoint(self, parent_run_id: str, paused_reservations: tuple[str, ...]) -> BudgetCheckpoint | None:
        """Save a complete pool only after every held reservation is safely paused."""
        owned = {key: value for key, value in self.reservations.items() if value.parent_run_id == parent_run_id}
        if not set(paused_reservations) <= set(owned) or any(
            not value.interrupted and key not in paused_reservations for key, value in owned.items()
        ):
            raise BudgetStateError("Every outstanding child must be paused before saving its parent budget.")
        for key in paused_reservations:
            self.reservations[key] = replace(self.reservations[key], interrupted=True)
        pool = self.pools.get(parent_run_id)
        if pool is None:
            return None
        return BudgetCheckpoint(
            parent_run_id=parent_run_id,
            state=BudgetState.model_validate(asdict(pool)),
            reservations=tuple(
                ReservationState(reservation_id=key, model_steps=value.model_steps, output_tokens=value.output_tokens)
                for key, value in sorted(owned.items())
            ),
        )

    def restore_checkpoint(self, checkpoint: BudgetCheckpoint) -> None:
        """Restore identical saved allowance without replenishing a live pool."""
        parent = checkpoint.parent_run_id
        state = checkpoint.state
        expected = ParentBudget(
            state.remaining_model_steps,
            state.remaining_output_tokens,
            state.parent_remaining_model_steps,
            state.parent_remaining_output_tokens,
            state.usage_charges,
        )
        reservations = {
            value.reservation_id: BudgetReservation(parent, value.model_steps, value.output_tokens, True)
            for value in checkpoint.reservations
        }
        owned = {key: value for key, value in self.reservations.items() if value.parent_run_id == parent}
        current = self.pools.get(parent)
        if current is not None and (current != expected or owned != reservations):
            raise BudgetStateError("Restored child checkpoint differs from the live parent pool.")
        if any(key in self.reservations and self.reservations[key] != value for key, value in reservations.items()):
            raise BudgetStateError("A restored reservation belongs to another parent Run.")
        self.pools[parent] = expected
        self.reservations.update(reservations)

    def restore(self, parent_run_id: str, state: BudgetState) -> None:
        """Restore one settled pool without expanding an existing allowance."""
        existing = self.snapshot(parent_run_id)
        if existing is not None and existing != state:
            raise BudgetStateError("Restored child budget differs from the live parent pool.")
        self.pools[parent_run_id] = ParentBudget(
            state.remaining_model_steps,
            state.remaining_output_tokens,
            state.parent_remaining_model_steps,
            state.parent_remaining_output_tokens,
            state.usage_charges,
        )

    def release(self, parent_run_id: str) -> None:
        """Release a terminal pool only after all child reservations settle."""
        self.snapshot(parent_run_id)
        self.pools.pop(parent_run_id, None)
