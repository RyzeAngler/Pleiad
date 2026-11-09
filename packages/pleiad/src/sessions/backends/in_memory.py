import asyncio
from collections.abc import Callable

from pleiad.sessions.errors import SessionConflictError
from pleiad.sessions.state import SessionState
from pleiad.sessions.store import SessionStore, descendant_session_ids


class InMemorySessionStore(SessionStore):
    """Provide the persistent-journal contract with process-local transactions."""

    def __init__(self) -> None:
        """Create an isolated store with one atomic mutation scope."""
        self.records: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def transact[T](self, session_id: str, mutation: Callable[[SessionState], tuple[SessionState, T]]) -> T:
        """Validate and commit one transition without leaking mutable stored data."""
        async with self.lock:
            payload = self.records.get(session_id)
            state = (
                SessionState(session_id=session_id) if payload is None else SessionState.model_validate_json(payload)
            )
            updated, result = mutation(state)
            if updated.session_id != session_id:
                raise SessionConflictError("A transaction cannot change its Session identity.")
            self.records[session_id] = updated.model_dump_json()
            return result

    async def snapshot(self, session_id: str) -> SessionState:
        """Read a detached validated journal snapshot."""
        async with self.lock:
            payload = self.records.get(session_id)
            return SessionState(session_id=session_id) if payload is None else SessionState.model_validate_json(payload)

    async def delete(self, session_id: str) -> None:
        """Delete stopped history while protecting an active or paused Run."""
        async with self.lock:
            payload = self.records.get(session_id)
            if payload is not None and SessionState.model_validate_json(payload).current_run_id is not None:
                raise SessionConflictError("A Session with active or paused work cannot be deleted.")
            self.records.pop(session_id, None)

    async def descendant_sessions(self, parent_run_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Read a consistent recursive ownership index under the backend lock."""
        async with self.lock:
            states = tuple(SessionState.model_validate_json(payload) for payload in self.records.values())
            return descendant_session_ids(states, parent_run_ids)
