from dataclasses import dataclass, field

from pleiad.budgets import BudgetLedger
from pleiad.contracts import UsageCharge
from pleiad.runtime.policy import EffectivePolicy
from pleiad.sessions.usage import RunUsage
from pleiad.tools.base import ToolScope
from pleiad.tools.contracts import ParentRunLimits


@dataclass(slots=True)
class RunControl:
    """Keep explicit owner intent separate from execution facts."""

    pause_requested: bool = False
    pause_reason: str = "user_requested"
    cancel_reason: str | None = None
    usage: RunUsage = field(default_factory=RunUsage)

    budget_ledger: BudgetLedger | None = None
    scopes: tuple[tuple[str, ToolScope], ...] = ()
    closed_scopes: dict[str, bool] = field(default_factory=dict)
    usage_charges: dict[str, UsageCharge] = field(default_factory=dict)

    def collect(self, run_id: str, usage: RunUsage, charges: tuple[UsageCharge, ...] = ()) -> RunUsage:
        """Merge each stable cumulative child fact once before admission or persistence."""
        ledger = self.budget_ledger
        for value in charges:
            if value.parent_run_id != run_id:
                raise ValueError("Child usage must belong to the invoking Run")
            previous = self.usage_charges.get(value.charge_id)
            if previous is not None and (
                value.model_steps < previous.model_steps or value.output_tokens < previous.output_tokens
            ):
                raise ValueError("Cumulative child usage cannot decrease")
            self.usage_charges[value.charge_id] = value
            if ledger is not None:
                ledger.record_charge(value)
        if ledger is not None:
            self.usage_charges.update((value.charge_id, value) for value in ledger.usage_charges(run_id))
        self.usage = usage.with_children(tuple(self.usage_charges.values()))
        return self.usage

    async def wait_children(self, run_id: str) -> None:
        """Wait for existing scoped children before deciding whether allowance is exhausted."""
        for binding in self.scopes:
            await binding[1].wait(run_id)

    def admission(self, run_id: str, usage: RunUsage, policy: EffectivePolicy) -> tuple[RunUsage, int, int]:
        """Intersect parent model admission with actual held child reservations."""
        current = self.collect(run_id, usage)
        steps = max(0, policy.max_model_steps - current.total_model_calls)
        tokens = max(0, policy.max_output_tokens - current.budget_output_tokens)
        if self.budget_ledger is not None:
            pool = self.budget_ledger.observe_parent(
                run_id,
                ParentRunLimits(
                    deadline=policy.deadline,
                    remaining_model_steps=steps,
                    remaining_output_tokens=tokens,
                    child_model_calls=current.child_model_calls,
                    child_budget_output_tokens=current.child_budget_output_tokens,
                    tool_concurrency=policy.tool_concurrency,
                ),
            )
            steps, tokens = max(0, pool.remaining_model_steps), max(0, pool.remaining_output_tokens)
        return current, steps, tokens

    async def close_scopes(self, run_id: str, *, cancelled: bool, suspended: bool) -> None:
        """Settle every resource owner and retain diagnostic failures after all cleanup."""
        failures: list[Exception] = []
        try:
            for code, scope in reversed(self.scopes):
                previous = self.closed_scopes.get(code)
                if previous is not None and (suspended or not previous):
                    continue
                scope_suspended = suspended and not failures
                try:
                    await scope.close(run_id, cancelled or bool(failures), scope_suspended)
                except Exception as error:
                    self.closed_scopes[code] = False
                    failures.append(error)
                else:
                    self.closed_scopes[code] = scope_suspended
        finally:
            self.collect(run_id, self.usage)
        if failures:
            raise ExceptionGroup("Scoped child cleanup failed", failures)
