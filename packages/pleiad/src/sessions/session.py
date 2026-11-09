from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.input import InputEnvelope, InputReceipt, InputRecord
from pleiad.sessions.model_step import ModelStep
from pleiad.sessions.records import RunSnapshot, SessionPage
from pleiad.sessions.store import SessionStore


class Session:
    """Expose a stable host-owned interaction identity and durable input inbox."""

    def __init__(self, session_id: str, store: SessionStore) -> None:
        """Bind an identity to a backend without acquiring execution ownership."""
        if not session_id.strip() or len(session_id) > 256:
            raise ValueError("session_id must contain between 1 and 256 characters")
        self.session_id = session_id
        self.store = store

    async def enqueue(self, value: InputEnvelope) -> InputReceipt:
        """Persist an input and return its original idempotent acceptance receipt."""
        return await self.store.enqueue(self.session_id, value)

    async def read(self, *, after_sequence: int = 0, limit: int = 50) -> SessionPage:
        """Read bounded original messages without granting Run writer ownership."""
        return await self.store.read(self.session_id, after_sequence=after_sequence, limit=limit)

    async def history(self) -> tuple[ModelStep, ...]:
        """Read only model-protocol-complete steps for request assembly."""
        return await self.store.history(self.session_id)

    async def checkpoint(self, run_id: str) -> RunCheckpoint | None:
        """Read a Run's currently saved paused position."""
        return (await self.store.run(self.session_id, run_id)).checkpoint

    async def run_for_invocation(self, invocation_id: str) -> RunSnapshot | None:
        """Recover an accepted invocation after its acknowledgement was lost."""
        return await self.store.run_for_invocation(self.session_id, invocation_id)

    async def pending_inputs(self) -> tuple[InputRecord, ...]:
        """Read accepted inputs that no Run has claimed."""
        return tuple(value for value in (await self.store.snapshot(self.session_id)).inputs if value.run_id is None)

    async def clear(self) -> None:
        """Delete this Session only when its writer lifecycle is fully settled."""
        await self.store.delete(self.session_id)
