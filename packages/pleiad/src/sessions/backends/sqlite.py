import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from pleiad.sessions.errors import SessionConflictError, SessionDataCorruptedError, SessionStorageError
from pleiad.sessions.state import SessionState
from pleiad.sessions.store import SessionStore, complete_owned, descendant_session_ids


class SqliteSessionStore(SessionStore):
    """Persist validated Session journals in independent SQLite transactions."""

    def __init__(self, database: Path) -> None:
        """Select an SDK-owned database without opening or migrating existing data."""
        self.database = database

    def connect(self) -> sqlite3.Connection:
        """Open a fresh connection and validate the exclusive SDK schema."""
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != 1:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if version == 1 and tables == {"session_journals"}:
                connection.commit()
                return connection
            if version != 0 or tables:
                raise SessionDataCorruptedError("Unsupported SessionStore schema. Select a fresh database.")
            connection.execute(
                "CREATE TABLE session_journals "
                "(session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    def load(self, connection: sqlite3.Connection, session_id: str) -> SessionState:
        """Validate a stored row and its independently checked revision."""
        row = connection.execute(
            "SELECT revision,payload FROM session_journals WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            return SessionState(session_id=session_id)
        try:
            state = SessionState.model_validate_json(row[1])
        except ValidationError as error:
            raise SessionDataCorruptedError("The persisted Session journal is invalid.") from error
        if state.session_id != session_id or state.revision != row[0]:
            raise SessionDataCorruptedError("Session identity or revision does not match its stored row.")
        return state

    def transact_sync[T](self, session_id: str, mutation: Callable[[SessionState], tuple[SessionState, T]]) -> T:
        """Commit a validated transition while holding the SQLite write reservation."""
        try:
            with closing(self.connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                state = self.load(connection, session_id)
                updated, result = mutation(state)
                if updated.session_id != session_id:
                    raise SessionConflictError("A transaction cannot change its Session identity.")
                if updated != state:
                    connection.execute(
                        "INSERT INTO session_journals(session_id,revision,payload) VALUES(?,?,?) "
                        "ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                        (session_id, updated.revision, updated.model_dump_json()),
                    )
                return result
        except (OSError, sqlite3.Error) as error:
            raise SessionStorageError(f"Session transaction failed for {session_id}.") from error

    async def transact[T](self, session_id: str, mutation: Callable[[SessionState], tuple[SessionState, T]]) -> T:
        """Keep SQLite work off the event loop while preserving atomic transitions."""
        return await complete_owned(asyncio.to_thread(self.transact_sync, session_id, mutation))

    def snapshot_sync(self, session_id: str) -> SessionState:
        """Load one committed journal from a fresh owned connection."""
        try:
            with closing(self.connect()) as connection:
                return self.load(connection, session_id)
        except (OSError, sqlite3.Error) as error:
            raise SessionStorageError(f"Session read failed for {session_id}.") from error

    async def snapshot(self, session_id: str) -> SessionState:
        """Read detached facts without blocking the event loop."""
        return await complete_owned(asyncio.to_thread(self.snapshot_sync, session_id))

    def delete_sync(self, session_id: str) -> None:
        """Delete only a stopped journal under the same writer transaction."""
        try:
            with closing(self.connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                if self.load(connection, session_id).current_run_id is not None:
                    raise SessionConflictError("A Session with active or paused work cannot be deleted.")
                connection.execute("DELETE FROM session_journals WHERE session_id=?", (session_id,))
        except (OSError, sqlite3.Error) as error:
            raise SessionStorageError(f"Session deletion failed for {session_id}.") from error

    async def delete(self, session_id: str) -> None:
        """Remove stopped history atomically without affecting other Sessions."""
        await complete_owned(asyncio.to_thread(self.delete_sync, session_id))

    def descendant_sessions_sync(self, parent_run_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Read and validate all ownership relations within one SQLite snapshot."""
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN")
                identities = tuple(row[0] for row in connection.execute("SELECT session_id FROM session_journals"))
                states = tuple(self.load(connection, identity) for identity in identities)
                return descendant_session_ids(states, parent_run_ids)
        except (OSError, sqlite3.Error) as error:
            raise SessionStorageError("Session descendant lookup failed.") from error

    async def descendant_sessions(self, parent_run_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Read recursive child ownership without blocking the event loop."""
        return await complete_owned(asyncio.to_thread(self.descendant_sessions_sync, parent_run_ids))
