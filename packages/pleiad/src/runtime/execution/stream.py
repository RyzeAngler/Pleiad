import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress

from pleiad.events.contracts import Event
from pleiad.runtime.lifecycle.result import RunResult


class RunStream:
    """Caller-owned event stream for a single Runner execution task."""

    def __init__(self, events_queue: asyncio.Queue[Event | None], task: asyncio.Task[RunResult]) -> None:
        """Bind the event queue to the task that owns the execution."""
        self.events_queue = events_queue
        self.task = task

    async def __aenter__(self) -> "RunStream":
        """Enter the caller-owned stream scope."""
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Deterministically close execution when leaving the stream scope."""
        await self.aclose()

    async def events(self) -> AsyncGenerator[Event, None]:
        """Yield ordered events and cancel the owned task on early close."""
        finished = False
        try:
            while True:
                if not self.events_queue.empty():
                    event = self.events_queue.get_nowait()
                elif self.task.done():
                    finished = True
                    await self.task
                    return
                else:
                    async with asyncio.TaskGroup() as group:
                        receive = group.create_task(self.events_queue.get())
                        await asyncio.wait((receive, self.task), return_when=asyncio.FIRST_COMPLETED)
                        if not receive.done():
                            receive.cancel()
                    if receive.cancelled():
                        continue
                    event = receive.result()
                if event is None:
                    finished = True
                    await self.task
                    return
                yield event
        finally:
            if not finished and not self.task.done():
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task

    async def result(self) -> RunResult:
        """Return the final result or propagate the owned task failure."""
        return await self.task

    async def aclose(self) -> None:
        """Cancel and await the execution task owned by this stream."""
        if self.task.done():
            with suppress(asyncio.CancelledError):
                await self.task
            return
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
