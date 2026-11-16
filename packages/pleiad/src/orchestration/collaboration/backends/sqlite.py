import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from pleiad.orchestration.collaboration.errors import CollaborationStorageError
from pleiad.orchestration.collaboration.state import CollaborationState
from pleiad.orchestration.collaboration.store import CollaborationStore, validate_write
from pleiad.sessions.store import complete_owned


class SqliteCollaborationStore(CollaborationStore):
    """Persist atomic scope journals in a database separate from SessionStore."""

    def __init__(self, database: Path) -> None:
        """Select a dedicated database without modifying existing schemas."""
        self.database = database

    def connect(self) -> sqlite3.Connection:
        """Open and validate the exclusive collaboration schema."""
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if version == 0 and not tables:
                connection.execute(
                    "CREATE TABLE collaboration_journals "
                    "(scope_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
                    "epoch INTEGER NOT NULL, payload TEXT NOT NULL)"
                )
                connection.execute("PRAGMA user_version=1")
            elif version != 1 or tables != {"collaboration_journals"}:
                raise CollaborationStorageError("Unsupported collaboration schema; select a dedicated database.")
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    def load(self, connection: sqlite3.Connection, scope_id: str) -> CollaborationState | None:
        """Validate persisted facts and their independently indexed owner version."""
        row = connection.execute(
            "SELECT revision,epoch,payload FROM collaboration_journals WHERE scope_id=?", (scope_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            state = CollaborationState.model_validate_json(row[2])
        except ValidationError as error:
            raise CollaborationStorageError("Invalid collaboration journal.") from error
        if state.scope_id != scope_id or state.revision != row[0] or state.owner_epoch != row[1]:
            raise CollaborationStorageError("Collaboration identity or version does not match its journal.")
        return state

    def save(self, connection: sqlite3.Connection, state: CollaborationState) -> None:
        """Write a validated journal inside the caller's owned transaction."""
        connection.execute(
            "INSERT INTO collaboration_journals(scope_id,revision,epoch,payload) VALUES(?,?,?,?) "
            "ON CONFLICT(scope_id) DO UPDATE SET revision=excluded.revision,"
            "epoch=excluded.epoch,payload=excluded.payload",
            (state.scope_id, state.revision, state.owner_epoch, state.model_dump_json()),
        )

    def read_sync(self, scope_id: str) -> CollaborationState | None:
        """Read using a fresh, locally owned database connection."""
        try:
            with closing(self.connect()) as connection:
                return self.load(connection, scope_id)
        except (OSError, sqlite3.Error) as error:
            raise CollaborationStorageError(f"Cannot read collaboration scope {scope_id}.") from error

    async def read(self, scope_id: str) -> CollaborationState | None:
        """Keep database work off the event loop."""
        return await complete_owned(asyncio.to_thread(self.read_sync, scope_id))

    def write_sync(self, state: CollaborationState) -> None:
        """Commit only the next revision from the current owner."""
        try:
            with closing(self.connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self.save(connection, validate_write(self.load(connection, state.scope_id), state))
        except (OSError, sqlite3.Error) as error:
            raise CollaborationStorageError(f"Cannot write collaboration scope {state.scope_id}.") from error

    async def write(self, state: CollaborationState) -> None:
        """Finish an accepted database write even if its awaiting caller is cancelled."""
        await complete_owned(asyncio.to_thread(self.write_sync, state))

    def claim_sync(self, scope_id: str, expected_epoch: int) -> CollaborationState:
        """Fence the previous owner without replaying any external action."""
        try:
            with closing(self.connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self.load(connection, scope_id)
                if current is None or current.owner_epoch != expected_epoch:
                    raise CollaborationStorageError("Cannot acquire the requested collaboration owner epoch.")
                state = current.model_copy(update={"owner_epoch": expected_epoch + 1, "revision": current.revision + 1})
                self.save(connection, state)
                return state
        except (OSError, sqlite3.Error) as error:
            raise CollaborationStorageError(f"Cannot claim collaboration scope {scope_id}.") from error

    async def claim(self, scope_id: str, expected_epoch: int) -> CollaborationState:
        """Acquire ownership only after the host has excluded the old deployment."""
        return await complete_owned(asyncio.to_thread(self.claim_sync, scope_id, expected_epoch))
