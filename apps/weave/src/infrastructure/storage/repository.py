"""Weave persistence repositories."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from pleiad.contracts import ContractModel, JsonObject
from pleiad.errors.base import SystemFault

from pleiad_weave.infrastructure.storage.models import (
    ExecutionEventInput,
    ExecutionEventRecord,
    ExecutionRecord,
    ExecutionResumeState,
    ExecutionStatus,
    GraphRunRecord,
    NodeRunRecord,
)

MAX_EXECUTION_EVENT_BYTES = 16_384


class JsonRecordRepository[RecordT: ContractModel]:
    """Small atomic JSON repository shared by runtime record types."""

    def __init__(self, root: Path, record_type: type[RecordT], key_field: str) -> None:
        self.root = root
        self.record_type = record_type
        self.key_field = key_field
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()

    def save(self, record: RecordT) -> RecordT:
        """Write one record using a same-directory replacement."""
        record_id = str(getattr(record, self.key_field))
        path = self.root / f"{record_id}.json"
        temporary = path.with_suffix(".json.tmp")
        try:
            with self.lock:
                temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
                temporary.replace(path)
        except OSError as exc:
            raise SystemFault(
                "weave.system_storage", str({"path": str(path), "exception": exc.__class__.__name__})
            ) from exc
        return record

    def get(self, record_id: str) -> RecordT | None:
        """Read one record if it exists."""
        path = self.root / f"{record_id}.json"
        try:
            with self.lock:
                if not path.is_file():
                    return None
                return self.record_type.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemFault(
                "weave.system_storage", str({"path": str(path), "exception": exc.__class__.__name__})
            ) from exc

    def list_records(self) -> tuple[RecordT, ...]:
        """Read durable attempt records while rejecting malformed stored data.

        Returns:
            Validated records ordered by their persisted file names.

        Raises:
            SystemFault: If a record cannot be read or violates its contract.
        """
        try:
            with self.lock:
                records: list[RecordT] = []
                for path in sorted(self.root.glob("*.json")):
                    record = self.record_type.model_validate_json(path.read_text(encoding="utf-8"))
                    if path.stem != str(getattr(record, self.key_field)):
                        raise ValueError("The persisted record identity does not match its file")
                    records.append(record)
                return tuple(records)
        except (OSError, ValueError) as error:
            raise SystemFault(
                "weave.system_storage", str({"path": str(self.root), "exception": type(error).__name__})
            ) from error


class ExecutionRepository:
    """Persist Execution rows and ordered events in one SQLite journal."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "execution-journal.sqlite3"
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        try:
            with closing(self.connect()) as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS executions (
                        execution_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision > 0),
                        record_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS execution_events (
                        execution_id TEXT NOT NULL REFERENCES executions(execution_id),
                        sequence INTEGER NOT NULL CHECK (sequence > 0),
                        event_id TEXT NOT NULL UNIQUE,
                        event_json TEXT NOT NULL,
                        PRIMARY KEY (execution_id, sequence)
                    );
                    CREATE TABLE IF NOT EXISTS execution_resume_states (
                        execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id),
                        state_json TEXT NOT NULL
                    );
                    """
                )
        except sqlite3.Error as exc:
            raise self.storage_error(exc) from exc

    def create(
        self,
        record: ExecutionRecord,
        *,
        event: ExecutionEventInput,
    ) -> tuple[ExecutionRecord, ExecutionEventRecord]:
        """Insert an Execution and its first lifecycle event in one transaction."""
        saved = record.model_copy(update={"revision": 1})
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO executions (execution_id, status, revision, record_json) VALUES (?, ?, ?, ?)",
                    (saved.execution_id, saved.status.value, saved.revision, saved.model_dump_json()),
                )
                saved_event = self.append_event_in_transaction(connection, saved.execution_id, event)
                connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise self.storage_error(exc) from exc
        return saved, saved_event

    def transition(
        self,
        record: ExecutionRecord,
        *,
        expected_revision: int,
        event: ExecutionEventInput,
        resume_state: ExecutionResumeState | None = None,
    ) -> tuple[ExecutionRecord, ExecutionEventRecord] | None:
        """CAS an Execution and append its lifecycle event atomically."""
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        saved = record.model_copy(update={"revision": expected_revision + 1})
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT record_json FROM executions WHERE execution_id = ? AND revision = ?",
                    (record.execution_id, expected_revision),
                ).fetchone()
                if row is None:
                    return None
                previous = ExecutionRecord.model_validate_json(row["record_json"])
                allowed = {
                    ExecutionStatus.QUEUED: {
                        ExecutionStatus.RUNNING,
                        ExecutionStatus.CANCELLED,
                        ExecutionStatus.FAILED,
                    },
                    ExecutionStatus.RUNNING: {
                        ExecutionStatus.WAITING_APPROVAL,
                        ExecutionStatus.SUCCEEDED,
                        ExecutionStatus.CANCELLED,
                        ExecutionStatus.FAILED,
                    },
                    ExecutionStatus.WAITING_APPROVAL: {
                        ExecutionStatus.RESUME_QUEUED,
                        ExecutionStatus.CANCELLED,
                        ExecutionStatus.FAILED,
                    },
                    ExecutionStatus.RESUME_QUEUED: {
                        ExecutionStatus.RUNNING,
                        ExecutionStatus.CANCELLED,
                        ExecutionStatus.FAILED,
                    },
                }
                if record.status not in allowed.get(previous.status, set()):
                    raise ValueError(f"Invalid Execution transition: {previous.status} to {record.status}")
                cursor = connection.execute(
                    "UPDATE executions SET status = ?, revision = ?, record_json = ? "
                    "WHERE execution_id = ? AND revision = ?",
                    (
                        saved.status.value,
                        saved.revision,
                        saved.model_dump_json(),
                        saved.execution_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                if resume_state is not None:
                    connection.execute(
                        "INSERT INTO execution_resume_states (execution_id, state_json) VALUES (?, ?) "
                        "ON CONFLICT(execution_id) DO UPDATE SET state_json = excluded.state_json",
                        (record.execution_id, resume_state.model_dump_json()),
                    )
                if record.status in {ExecutionStatus.WAITING_APPROVAL, ExecutionStatus.RESUME_QUEUED}:
                    recovery = connection.execute(
                        "SELECT state_json FROM execution_resume_states WHERE execution_id = ?", (record.execution_id,)
                    ).fetchone()
                    if recovery is None:
                        raise ValueError("A paused Execution requires persisted recovery state")
                saved_event = self.append_event_in_transaction(connection, saved.execution_id, event)
                connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise self.storage_error(exc) from exc
        return saved, saved_event

    def resume_state(self, execution_id: str) -> ExecutionResumeState | None:
        """Read recovery data without exposing it in the public Execution summary."""
        try:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    "SELECT state_json FROM execution_resume_states WHERE execution_id = ?", (execution_id,)
                ).fetchone()
            return ExecutionResumeState.model_validate_json(row["state_json"]) if row is not None else None
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise self.storage_error(exc) from exc

    def settled_resume_states(self) -> tuple[tuple[ExecutionRecord, ExecutionResumeState], ...]:
        """Find exact Turn ownership after a crash between terminal commit and cleanup."""
        try:
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    "SELECT executions.record_json, execution_resume_states.state_json "
                    "FROM executions JOIN execution_resume_states USING(execution_id) "
                    "WHERE executions.status IN (?, ?, ?) ORDER BY executions.rowid",
                    (ExecutionStatus.SUCCEEDED.value, ExecutionStatus.FAILED.value, ExecutionStatus.CANCELLED.value),
                ).fetchall()
            return tuple(
                (
                    ExecutionRecord.model_validate_json(row["record_json"]),
                    ExecutionResumeState.model_validate_json(row["state_json"]),
                )
                for row in rows
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise self.storage_error(exc) from exc

    def get(self, execution_id: str) -> ExecutionRecord | None:
        """Read one current Execution snapshot."""
        try:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    "SELECT record_json FROM executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
            return ExecutionRecord.model_validate_json(row["record_json"]) if row is not None else None
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise self.storage_error(exc) from exc

    def list_nonterminal(self) -> tuple[ExecutionRecord, ...]:
        """Return persisted Executions that have not reached a terminal status."""
        terminal = tuple(
            status.value
            for status in (
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            )
        )
        try:
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    "SELECT record_json FROM executions WHERE status NOT IN (?, ?, ?) ORDER BY rowid",
                    terminal,
                ).fetchall()
            return tuple(ExecutionRecord.model_validate_json(row["record_json"]) for row in rows)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise self.storage_error(exc) from exc

    def settled_execution_ids(self) -> frozenset[str]:
        """Identify settled owners whose subordinate attempts must no longer run.

        Returns:
            Durable identities of succeeded, failed, or cancelled Executions.

        Raises:
            SystemFault: If the Execution journal cannot be read.
        """
        try:
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    "SELECT execution_id FROM executions WHERE status IN (?, ?, ?)",
                    (ExecutionStatus.SUCCEEDED.value, ExecutionStatus.FAILED.value, ExecutionStatus.CANCELLED.value),
                ).fetchall()
            return frozenset(str(row["execution_id"]) for row in rows)
        except (OSError, sqlite3.Error) as error:
            raise self.storage_error(error) from error

    def append_event(
        self,
        execution_id: str,
        event_type: str,
        data: JsonObject,
        *,
        event_id: str | None = None,
        timestamp: datetime | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> ExecutionEventRecord:
        """Append an event with the next sequence for its Execution."""
        event_input = ExecutionEventInput(
            event_type=event_type,
            data=data,
            event_id=event_id,
            timestamp=timestamp,
            trace_id=trace_id,
            span_id=span_id,
        )
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                event = self.append_event_in_transaction(connection, execution_id, event_input)
                connection.commit()
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise self.storage_error(exc) from exc
        return event

    def append_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        execution_id: str,
        event_input: ExecutionEventInput,
    ) -> ExecutionEventRecord:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM execution_events WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        event = ExecutionEventRecord(
            event_id=event_input.event_id or uuid4().hex,
            execution_id=execution_id,
            sequence=int(row["next_sequence"]),
            event_type=event_input.event_type,
            data=event_input.data,
            timestamp=event_input.timestamp or datetime.now(UTC),
            trace_id=event_input.trace_id,
            span_id=event_input.span_id,
        )
        serialized_event = event.model_dump_json()
        serialized_bytes = len(serialized_event.encode("utf-8"))
        if serialized_bytes > MAX_EXECUTION_EVENT_BYTES:
            raise SystemFault(
                "weave.system_storage",
                str(
                    {
                        "reason": "execution event exceeds maximum serialized size",
                        "maximum_bytes": MAX_EXECUTION_EVENT_BYTES,
                        "actual_bytes": serialized_bytes,
                    }
                ),
            )
        connection.execute(
            "INSERT INTO execution_events (execution_id, sequence, event_id, event_json) VALUES (?, ?, ?, ?)",
            (execution_id, event.sequence, event.event_id, serialized_event),
        )
        return event

    def read_events(
        self,
        execution_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[ExecutionEventRecord, ...]:
        """Read ordered events strictly after the given sequence cursor."""
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        try:
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    "SELECT event_json FROM execution_events WHERE execution_id = ? AND sequence > ? ORDER BY sequence",
                    (execution_id, after_sequence),
                ).fetchall()
            return tuple(ExecutionEventRecord.model_validate_json(row["event_json"]) for row in rows)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise self.storage_error(exc) from exc

    def storage_error(self, exc: Exception) -> SystemFault:
        return SystemFault("weave.system_storage", str({"path": str(self.path), "exception": exc.__class__.__name__}))


class GraphRunRepository(JsonRecordRepository[GraphRunRecord]):
    """Persist Graph revision runtime state independently from definitions."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, GraphRunRecord, "graph_run_id")


class NodeRunRepository(JsonRecordRepository[NodeRunRecord]):
    """Persist temporary Node Agent run state independently from graph definitions."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, NodeRunRecord, "node_run_id")
