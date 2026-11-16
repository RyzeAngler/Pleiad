import asyncio

from pleiad.orchestration.collaboration.errors import CollaborationStorageError
from pleiad.orchestration.collaboration.state import CollaborationState
from pleiad.orchestration.collaboration.store import CollaborationStore, validate_write


class InMemoryCollaborationStore(CollaborationStore):
    """Implement the same fenced journal contract without external persistence."""

    def __init__(self) -> None:
        """Create a private collection of scope journals."""
        self.states: dict[str, CollaborationState] = {}
        self.lock = asyncio.Lock()

    async def read(self, scope_id: str) -> CollaborationState | None:
        """Return detached facts so callers cannot mutate committed state."""
        async with self.lock:
            state = self.states.get(scope_id)
            return None if state is None else state.model_copy(deep=True)

    async def write(self, state: CollaborationState) -> None:
        """Atomically enforce revision and owner fencing."""
        async with self.lock:
            self.states[state.scope_id] = validate_write(self.states.get(state.scope_id), state)

    async def claim(self, scope_id: str, expected_epoch: int) -> CollaborationState:
        """Advance ownership once for an exclusively recovered deployment."""
        async with self.lock:
            current = self.states.get(scope_id)
            if current is None or current.owner_epoch != expected_epoch:
                raise CollaborationStorageError("Cannot acquire the requested collaboration owner epoch.")
            state = current.model_copy(update={"owner_epoch": expected_epoch + 1, "revision": current.revision + 1})
            self.states[scope_id] = state
            return state.model_copy(deep=True)
