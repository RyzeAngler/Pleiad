import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from pleiad.runtime.events import RunEvent
from pleiad.runtime.execution.control_state import RunControl
from pleiad.runtime.result import RunOutcome, RunResult, RunSuspension
from pleiad.sessions.session import Session


class RunHandle:
    """Own one active execution scope independently of event subscribers."""

    def __init__(
        self,
        session: Session,
        run_id: str,
        control: RunControl,
        cancel_paused: Callable[[str], Awaitable[RunResult]],
        outcome: RunOutcome | None = None,
    ) -> None:
        """Bind the persisted Run and the callback for cancelling a suspension."""
        self.session = session
        self.run_id = run_id
        self.control = control
        self.cancel_paused = cancel_paused
        self.outcome = outcome
        self.task: asyncio.Task[RunOutcome] | None = None
        self.condition = asyncio.Condition()
        self.cancel_lock = asyncio.Lock()
        self.started = asyncio.Event()
        self.finished = outcome is not None

    def launch(self, execution: Callable[[], Awaitable[RunOutcome]]) -> None:
        """Start the sole task owned by this handle.

        Args:
            execution: Fenced execution that commits its result before returning.

        Raises:
            RuntimeError: If execution was already launched or settled.
        """
        if self.task is not None or self.outcome is not None:
            raise RuntimeError("The Run handle already owns an execution")

        async def own() -> RunOutcome:
            try:
                self.started.set()
                self.outcome = await execution()
                return self.outcome
            finally:
                self.finished = True
                await self.notify()

        self.task = asyncio.create_task(own(), name=f"pleiad-run-{self.run_id}")

    async def notify(self) -> None:
        """Wake event observers after a durable commit."""
        async with self.condition:
            self.condition.notify_all()

    async def events(self, *, after_sequence: int = 0) -> AsyncIterator[RunEvent]:
        """Replay persisted events and observe new commits without owning execution.

        Args:
            after_sequence: Exclusive persisted event cursor.

        Yields:
            Ordered committed observations. Leaving iteration only detaches this observer.

        Raises:
            ValueError: If the supplied cursor is negative.
        """
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        cursor = after_sequence
        while True:
            async with self.condition:
                records = await self.session.store.read_events(
                    self.session.session_id, self.run_id, after_sequence=cursor, limit=256
                )
                done = self.finished or self.task is None
                if not records:
                    if done:
                        return
                    await self.condition.wait()
                    continue
            for record in records:
                cursor = record.sequence
                yield RunEvent(sequence=record.sequence, event=record.data)

    async def result(self) -> RunOutcome:
        """Wait for a committed outcome while preserving owner cancellation semantics."""
        if self.outcome is not None and (self.task is None or self.task.done()):
            return self.outcome
        if self.task is not None:
            try:
                return await asyncio.shield(self.task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                snapshot = await self.session.store.run(self.session.session_id, self.run_id)
                if snapshot.receipt is None:
                    raise
                self.outcome = RunResult(snapshot.receipt)
                return self.outcome
        if self.outcome is None:
            raise RuntimeError("This handle has no local execution owner")
        return self.outcome

    async def cancel(self, *, reason: str) -> RunResult:
        """Request cancellation and wait for owned resource cleanup and settlement."""
        if not reason.strip():
            raise ValueError("Cancellation requires a reason")
        async with self.cancel_lock:
            if self.control.cancel_reason is None:
                self.control.cancel_reason = reason
            task = self.task
            if task is not None and not task.done():
                await self.started.wait()
                if not task.cancelling():
                    task.cancel()
            outcome = await self.result()
            if isinstance(outcome, RunSuspension):
                await self.control.close_scopes(self.run_id, cancelled=True, suspended=False)
                settled = await self.cancel_paused(self.control.cancel_reason)
                self.outcome = settled
                return settled
            return outcome

    async def pause(self, *, reason: str) -> RunOutcome:
        """Close new-action admission and await the next safe persisted boundary."""
        if not reason.strip():
            raise ValueError("Pausing requires a reason")
        self.control.pause_requested = True
        self.control.pause_reason = reason
        return await self.result()

    async def aclose(self) -> None:
        """Release the active execution owned by this handle after cleanup."""
        if self.task is not None and not self.task.done():
            await self.cancel(reason="owner_closed")

    async def __aenter__(self) -> "RunHandle":
        """Enter the explicit execution-owner scope."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close active resources before leaving the owner scope."""
        await self.aclose()
