from collections import Counter as Counts
from collections.abc import Iterable
from datetime import UTC, datetime

from opentelemetry.metrics import CallbackOptions, Counter, Histogram, Meter, Observation, UpDownCounter

from pleiad_weave.infrastructure.storage.product import ProductStore
from pleiad_weave.infrastructure.storage.product_models import Attention, Execution, InboxItem, InputDisposition


class ProductMetrics:
    """Expose product scheduling measurements through the host's existing OTel meter."""

    def __init__(self, meter: Meter, store: ProductStore) -> None:
        """Create bounded-label instruments without installing an exporter."""
        self.store = store
        self.input_count: Counter = meter.create_counter("weave.input.acceptance.requests", unit="{input}")
        self.input_duration: Histogram = meter.create_histogram("weave.input.acceptance.duration", unit="s")
        self.delivery_failures: Counter = meter.create_counter("weave.input.delivery.failures", unit="{failure}")
        self.router_batches: Counter = meter.create_counter("weave.router.attempts", unit="{batch}")
        self.router_batch_size: Histogram = meter.create_histogram("weave.router.batch.size", unit="{input}")
        self.router_duration: Histogram = meter.create_histogram("weave.router.duration", unit="s")
        self.workers: UpDownCounter = meter.create_up_down_counter("weave.workers.active", unit="{worker}")
        self.silent_reports: Counter = meter.create_counter("weave.report.silent.requests", unit="{report}")
        self.control_duration: Histogram = meter.create_histogram("weave.control.duration", unit="s")
        self.cancellation_duration: Histogram = meter.create_histogram("weave.cancellation.cleanup.duration", unit="s")
        self.unknown_actions: Histogram = meter.create_histogram("weave.recovery.unknown_actions", unit="{action}")
        self.content_bytes: Histogram = meter.create_histogram("weave.content.transferred", unit="By")
        self.snapshot_resyncs: Counter = meter.create_counter("weave.snapshot.resync.requests", unit="{resync}")
        self.cleanup_attempts: Counter = meter.create_counter("weave.cleanup.attempts", unit="{attempt}")
        meter.create_observable_gauge("weave.inbox.pending", [self.queues], unit="{input}")
        meter.create_observable_gauge("weave.inbox.oldest_age", [self.queue_age], unit="s")
        meter.create_observable_gauge("weave.attention.pending", [self.attentions], unit="{attention}")
        meter.create_observable_gauge("weave.attention.oldest_age", [self.attention_age], unit="s")
        meter.create_observable_gauge("weave.input.responsibilities", [self.responsibilities], unit="{input}")
        meter.create_observable_gauge("weave.input.owned.oldest_age", [self.responsibility_age], unit="s")
        meter.create_observable_gauge("weave.input.reroute.rounds", [self.reroutes], unit="{round}")
        meter.create_observable_gauge("weave.execution.budget", [self.budgets], unit="{token}")
        meter.create_observable_gauge("weave.execution.states", [self.executions], unit="{execution}")
        meter.create_observable_gauge("weave.recovery.unsettled", [self.unsettled], unit="{action}")
        meter.create_observable_counter("weave.product.events", [self.events], unit="{event}")

    def queues(self, options: CallbackOptions) -> Iterable[Observation]:
        """Measure unhandled inputs by bounded host source and recipient kind."""
        with self.store.transaction() as connection:
            counts = Counts(
                (value.target_kind, value.wake_policy, value.source)
                for value in self.store.records(connection, "inbox_items", InboxItem)
                if value.status != "handled"
            )
        for (target, wake, source), count in counts.items():
            yield Observation(count, {"target": target, "wake_policy": wake, "source": source})

    def queue_age(self, options: CallbackOptions) -> Iterable[Observation]:
        """Measure actual oldest unhandled input age without resetting it at restart."""
        now = datetime.now(UTC)
        with self.store.transaction() as connection:
            items = self.store.records(connection, "inbox_items", InboxItem)
        for target in ("router", "execution"):
            ages = [
                (now - value.created_at).total_seconds()
                for value in items
                if value.target_kind == target and value.status != "handled"
            ]
            yield Observation(max(ages, default=0), {"target": target})

    def attentions(self, options: CallbackOptions) -> Iterable[Observation]:
        """Measure independently persisted user decisions by their actual kind."""
        with self.store.transaction() as connection:
            counts = Counts(
                value.kind
                for value in self.store.records(connection, "attentions", Attention)
                if value.status == "pending"
            )
        for kind in ("clarification", "approval", "review", "recovery"):
            yield Observation(counts[kind], {"kind": kind})

    def attention_age(self, options: CallbackOptions) -> Iterable[Observation]:
        """Read pending decision age from its committed creation event."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT MIN(e.created_at) AS oldest FROM product_events e "
                "JOIN attentions a ON a.id=e.subject WHERE a.status='pending' AND e.type='attention.created'"
            ).fetchone()
        oldest = row["oldest"]
        age = 0.0 if oldest is None else (datetime.now(UTC) - datetime.fromisoformat(oldest)).total_seconds()
        yield Observation(max(0, age))

    def responsibilities(self, options: CallbackOptions) -> Iterable[Observation]:
        """Keep Worker responsibility counts separate from SDK consumption."""
        with self.store.transaction() as connection:
            counts = Counts(
                value.state for value in self.store.records(connection, "input_dispositions", InputDisposition)
            )
        for state in ("owned", "handled", "returned"):
            yield Observation(counts[state], {"state": state})

    def responsibility_age(self, options: CallbackOptions) -> Iterable[Observation]:
        """Retain original input age while a target still owes its disposition."""
        now = datetime.now(UTC)
        with self.store.transaction() as connection:
            owned = {
                value.origin_input_id
                for value in self.store.records(connection, "input_dispositions", InputDisposition)
                if value.state == "owned"
            }
            ages = [
                (now - value.created_at).total_seconds()
                for value in self.store.records(connection, "inbox_items", InboxItem)
                if value.input_id in owned
            ]
        yield Observation(max(ages, default=0))

    def reroutes(self, options: CallbackOptions) -> Iterable[Observation]:
        """Measure committed responsibility rounds independently of Router retries."""
        with self.store.transaction() as connection:
            count = sum(
                value.reroute_round
                for value in self.store.records(connection, "inbox_items", InboxItem)
                if value.input_id == value.origin_input_id
            )
        yield Observation(count)

    def budgets(self, options: CallbackOptions) -> Iterable[Observation]:
        """Expose durable product allowance, consumed usage, and active reservation totals."""
        with self.store.transaction() as connection:
            values = self.store.records(connection, "executions", Execution)
        for phase in ("limit", "consumed", "reserved"):
            totals: dict[tuple[str, str], int] = {}
            for value in values:
                amount = {
                    "limit": value.budget_limit,
                    "consumed": value.budget_consumed,
                    "reserved": value.budget_reserved,
                }[phase]
                key = (value.status.value, "root" if value.parent_execution_id is None else "child")
                totals[key] = totals.get(key, 0) + amount
            for (status, scope), amount in totals.items():
                yield Observation(amount, {"phase": phase, "status": status, "scope": scope})

    def events(self, options: CallbackOptions) -> Iterable[Observation]:
        """Count committed handbacks, reports, cleanup and resync facts across restarts."""
        with self.store.transaction() as connection:
            rows = connection.execute("SELECT type,COUNT(*) AS count FROM product_events GROUP BY type").fetchall()
        for row in rows:
            yield Observation(row["count"], {"type": row["type"]})

    def executions(self, options: CallbackOptions) -> Iterable[Observation]:
        """Measure product job states independently of SDK invocation states."""
        with self.store.transaction() as connection:
            counts = Counts(value.status.value for value in self.store.records(connection, "executions", Execution))
        for state, count in counts.items():
            yield Observation(count, {"status": state})

    def unsettled(self, options: CallbackOptions) -> Iterable[Observation]:
        """Measure the current durable unknown scope independently of historical recovery attempts."""
        with self.store.transaction() as connection:
            count = sum(len(value.unknown_effects) for value in self.store.records(connection, "executions", Execution))
        yield Observation(count)
