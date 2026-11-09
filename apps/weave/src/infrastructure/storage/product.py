import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pleiad.contracts import ContractModel, JsonObject
from pleiad.errors.base import SystemFault
from pleiad.sessions.records import ReconcileRecord, ToolCallRecord
from pleiad.sessions.recovery import ActionRef, ReconcileResult
from pydantic import TypeAdapter

from pleiad_weave.infrastructure.storage.model_inputs import ROUTER_BATCH_SOURCE_BYTES
from pleiad_weave.infrastructure.storage.product_models import (
    MAX_TARGET_EXECUTIONS,
    TERMINAL_STATUSES,
    AnswerQuestionAction,
    Artifact,
    ArtifactMetadata,
    Attachment,
    Attention,
    AttentionView,
    BudgetReservation,
    ChatMessageRequest,
    ContextReference,
    CreateSpaceAction,
    DeliverInputAction,
    Delivery,
    DeliveryView,
    DispatchAction,
    Execution,
    ExecutionRun,
    ExecutionRunPage,
    ExecutionSpec,
    ExecutionStatus,
    ExecutionView,
    Handback,
    HandbackResolution,
    InboxItem,
    InputDisposition,
    InputDispositionView,
    Message,
    MessageReceipt,
    MessageView,
    Page,
    ProductCapacityError,
    ProductConflict,
    ProductEvent,
    Report,
    ReportSummary,
    RequestCancelAction,
    RequestClarificationAction,
    ResolutionReceipt,
    ResolutionRequest,
    RouteAttempt,
    RouteDecision,
    RouteReceipt,
    RoutingStatus,
    Snapshot,
    Space,
    SpaceConfiguration,
    SpaceView,
)

JSON_OBJECT = TypeAdapter[JsonObject](JsonObject)
ARTIFACT_IDS = TypeAdapter[tuple[str, ...]](tuple[str, ...])
TABLES = frozenset(
    {
        "spaces",
        "executions",
        "inbox_items",
        "deliveries",
        "input_dispositions",
        "input_handbacks",
        "input_handback_resolutions",
        "route_attempts",
        "reports",
        "attentions",
        "attachments",
        "artifacts",
        "cleanup_records",
        "budget_reservations",
        "execution_runs",
        "deletion_previews",
        "graph_definitions",
        "graph_runs",
        "node_attempts",
        "tool_results",
        "source_states",
        "source_observations",
    }
)
TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.QUEUED: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.PAUSED,
            ExecutionStatus.BLOCKED,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.WAITING_INPUT,
            ExecutionStatus.WAITING_APPROVAL,
            ExecutionStatus.WAITING_CHILDREN,
            ExecutionStatus.PAUSED,
            ExecutionStatus.BLOCKED,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.WAITING_INPUT: frozenset(
        {
            ExecutionStatus.QUEUED,
            ExecutionStatus.WAITING_APPROVAL,
            ExecutionStatus.WAITING_CHILDREN,
            ExecutionStatus.PAUSED,
            ExecutionStatus.BLOCKED,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.WAITING_APPROVAL: frozenset(
        {
            ExecutionStatus.QUEUED,
            ExecutionStatus.WAITING_INPUT,
            ExecutionStatus.WAITING_CHILDREN,
            ExecutionStatus.PAUSED,
            ExecutionStatus.BLOCKED,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.WAITING_CHILDREN: frozenset(
        {
            ExecutionStatus.QUEUED,
            ExecutionStatus.WAITING_INPUT,
            ExecutionStatus.WAITING_APPROVAL,
            ExecutionStatus.PAUSED,
            ExecutionStatus.BLOCKED,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.PAUSED: frozenset(
        {
            ExecutionStatus.QUEUED,
            ExecutionStatus.WAITING_INPUT,
            ExecutionStatus.WAITING_APPROVAL,
            ExecutionStatus.WAITING_CHILDREN,
            ExecutionStatus.BLOCKED,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.BLOCKED: frozenset(
        {
            ExecutionStatus.QUEUED,
            ExecutionStatus.WAITING_INPUT,
            ExecutionStatus.WAITING_APPROVAL,
            ExecutionStatus.WAITING_CHILDREN,
            ExecutionStatus.PAUSED,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.CANCELLING: frozenset({ExecutionStatus.CANCELLED, ExecutionStatus.BLOCKED}),
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.CANCELLED: frozenset(),
}


def stable_id(value: str) -> str:
    """Derive an opaque stable identity from an immutable product source key."""
    return uuid5(NAMESPACE_URL, f"pleiad-weave/{value}").hex


def fingerprint(value: ContractModel | JsonObject) -> str:
    """Hash a complete canonical request without exposing its original payload."""
    payload = value.model_dump(mode="json") if isinstance(value, ContractModel) else value
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class ProductStore:
    """Own all product transactions in one versioned SQLite database.

    Args:
        root: New-format runtime directory, separate from SDK Session storage.
        queue_capacity: Maximum pending inputs per target.
    """

    def __init__(self, root: Path, *, queue_capacity: int = 1_000) -> None:
        if queue_capacity < 1:
            raise ValueError("Queue capacity must be positive")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "product.sqlite3"
        self.content_root = self.root / "content"
        self.content_root.mkdir(exist_ok=True)
        if not self.content_root.resolve().is_relative_to(self.root):
            raise ValueError("Product content must remain inside the owned runtime root")
        self.queue_capacity = queue_capacity
        with closing(self.connect()) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                return
            if (
                version != 0
                or connection.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchone() is not None
            ):
                raise ValueError("Unsupported product database schema; use a fresh runtime-v2 directory")
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE spaces(id TEXT PRIMARY KEY,
                     revision INTEGER NOT NULL,
                     status TEXT NOT NULL,
                     payload TEXT NOT NULL);
                CREATE TABLE executions(id TEXT PRIMARY KEY,
                     space_id TEXT NOT NULL REFERENCES spaces(id),
                     parent_id TEXT REFERENCES executions(id),
                     session_id TEXT NOT NULL UNIQUE,
                     status TEXT NOT NULL,
                     revision INTEGER NOT NULL,
                     payload TEXT NOT NULL);
                CREATE TABLE chat_messages(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                     id TEXT NOT NULL UNIQUE,
                     source_key TEXT NOT NULL UNIQUE,
                     payload TEXT NOT NULL);
                CREATE TABLE inbox_items(id TEXT PRIMARY KEY,
                     origin_id TEXT NOT NULL,
                     target_kind TEXT NOT NULL,
                     target_id TEXT NOT NULL,
                     sequence INTEGER NOT NULL,
                     status TEXT NOT NULL,
                     wake TEXT NOT NULL,
                     payload TEXT NOT NULL,
                     UNIQUE(target_kind,
                    target_id,
                    sequence));
                CREATE TABLE deliveries(id TEXT PRIMARY KEY,
                     input_id TEXT NOT NULL UNIQUE REFERENCES inbox_items(id),
                     state TEXT NOT NULL,
                     generation INTEGER NOT NULL,
                     payload TEXT NOT NULL);
                CREATE TABLE input_dispositions(id TEXT PRIMARY KEY,
                     origin_id TEXT NOT NULL,
                     target_id TEXT NOT NULL REFERENCES executions(id),
                     input_id TEXT NOT NULL UNIQUE REFERENCES inbox_items(id),
                     state TEXT NOT NULL,
                     payload TEXT NOT NULL,
                     UNIQUE(origin_id,
                    target_id));
                CREATE TABLE input_handbacks(id TEXT PRIMARY KEY,
                     origin_id TEXT NOT NULL,
                     target_id TEXT NOT NULL,
                     payload TEXT NOT NULL,
                     UNIQUE(origin_id,
                    target_id));
                CREATE TABLE input_handback_resolutions(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE route_batches(decision_id TEXT PRIMARY KEY,
                     batch_digest TEXT NOT NULL UNIQUE,
                     attempt INTEGER NOT NULL,
                     state TEXT NOT NULL,
                     receipt TEXT);
                CREATE TABLE route_attempts(id TEXT PRIMARY KEY,
                     decision_id TEXT NOT NULL REFERENCES route_batches(decision_id),
                     invocation_id TEXT NOT NULL UNIQUE,
                     payload TEXT NOT NULL);
                CREATE TABLE reports(id TEXT PRIMARY KEY,
                     execution_id TEXT NOT NULL REFERENCES executions(id),
                     payload TEXT NOT NULL);
                CREATE TABLE attentions(id TEXT PRIMARY KEY,
                     execution_id TEXT REFERENCES executions(id),
                     status TEXT NOT NULL,
                     payload TEXT NOT NULL);
                CREATE TABLE attachments(id TEXT PRIMARY KEY,
                     client_id TEXT NOT NULL UNIQUE,
                     digest TEXT NOT NULL,
                     payload TEXT NOT NULL);
                CREATE TABLE artifacts(id TEXT PRIMARY KEY,
                     owner_id TEXT NOT NULL REFERENCES executions(id),
                     digest TEXT NOT NULL,
                     payload TEXT NOT NULL);
                CREATE TABLE cleanup_records(id TEXT PRIMARY KEY,
                     target_id TEXT NOT NULL UNIQUE REFERENCES spaces(id),
                     generation TEXT NOT NULL UNIQUE,
                     payload TEXT NOT NULL);
                CREATE TABLE budget_reservations(id TEXT PRIMARY KEY,
                     parent_id TEXT NOT NULL REFERENCES executions(id),
                     child_id TEXT NOT NULL UNIQUE REFERENCES executions(id),
                     payload TEXT NOT NULL);
                CREATE TABLE execution_runs(id TEXT PRIMARY KEY,
                     execution_id TEXT NOT NULL REFERENCES executions(id),
                     invocation_id TEXT NOT NULL UNIQUE,
                     payload TEXT NOT NULL);
                CREATE TABLE deletion_previews(id TEXT PRIMARY KEY,
                     payload TEXT NOT NULL);
                CREATE TABLE graph_definitions(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE graph_runs(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE node_attempts(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE tool_results(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE source_states(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE source_observations(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE request_receipts(request_key TEXT PRIMARY KEY,
                     digest TEXT NOT NULL,
                     result TEXT NOT NULL);
                CREATE TABLE product_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    subject TEXT NOT NULL,
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE object_references(owner_kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    PRIMARY KEY(owner_kind,
                    owner_id,
                    target_kind,
                    target_id));
                CREATE INDEX executions_status ON executions(status);
                CREATE INDEX executions_space ON executions(space_id);
                CREATE INDEX executions_parent ON executions(parent_id);
                CREATE INDEX inbox_target ON inbox_items(target_kind,target_id,status,sequence);
                CREATE INDEX deliveries_state ON deliveries(state);
                CREATE INDEX attentions_status ON attentions(status);
                PRAGMA user_version=1;
                COMMIT;
            """)

    def connect(self) -> sqlite3.Connection:
        """Open a bounded SQLite connection with referential integrity enabled."""
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Commit all owned facts together or roll back every effect on failure."""
        try:
            with closing(self.connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
        except sqlite3.Error as error:
            raise SystemFault("weave.product_storage", "Product persistence failed") from error

    def load[T: ContractModel](
        self, connection: sqlite3.Connection, table: str, identity: str, model: type[T]
    ) -> T | None:
        """Read a validated typed record from a fixed product table."""
        if table not in TABLES:
            raise ValueError("Unknown product record table")
        row = connection.execute(f"SELECT payload FROM {table} WHERE id=?", (identity,)).fetchone()  # noqa: S608 - Table names are checked against a fixed allowlist.
        return None if row is None else model.model_validate_json(row["payload"])

    def require[T: ContractModel](self, connection: sqlite3.Connection, table: str, identity: str, model: type[T]) -> T:
        """Read a required product record or fail with its missing identity."""
        result = self.load(connection, table, identity, model)
        if result is None:
            raise LookupError(f"{table} record not found: {identity}")
        return result

    def records[T: ContractModel](self, connection: sqlite3.Connection, table: str, model: type[T]) -> tuple[T, ...]:
        """Read validated records in stable insertion order within one transaction."""
        if table not in TABLES:
            raise ValueError("Unknown product record table")
        return tuple(
            model.model_validate_json(row["payload"])
            for row in connection.execute(f"SELECT payload FROM {table} ORDER BY rowid")  # noqa: S608 - Fixed table allowlist.
        )

    def save(
        self,
        connection: sqlite3.Connection,
        table: str,
        identity: str,
        record: ContractModel,
        **columns: str | int | None,
    ) -> None:
        """Upsert one already validated record and its queryable ownership columns."""
        if table not in TABLES or any(not name.isidentifier() for name in columns):
            raise ValueError("Invalid product table or column")
        validated = type(record).model_validate(record.model_dump())
        names = ("id", "payload", *columns)
        values = (identity, validated.model_dump_json(), *columns.values())
        assignments = ",".join(f"{name}=excluded.{name}" for name in names[1:])
        query = (
            f"INSERT INTO {table} ({','.join(names)}) "  # noqa: S608 - Fixed allowlist and identifier-only columns.
            f"VALUES ({','.join('?' for name in names)}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}"
        )
        connection.execute(query, values)

    def receipt[T: ContractModel](
        self, connection: sqlite3.Connection, key: str, digest: str, model: type[T]
    ) -> T | None:
        """Replay a completed request only when its full payload matches."""
        row = connection.execute("SELECT digest,result FROM request_receipts WHERE request_key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["digest"] != digest:
            raise ProductConflict("The request identity was already used with different content")
        return model.model_validate_json(row["result"])

    def save_receipt(self, connection: sqlite3.Connection, key: str, digest: str, result: ContractModel) -> None:
        """Commit the immutable request result with its associated product facts."""
        connection.execute("INSERT INTO request_receipts VALUES(?,?,?)", (key, digest, result.model_dump_json()))

    def emit(self, connection: sqlite3.Connection, subject: str, kind: str, data: JsonObject) -> None:
        """Append one event only within the transaction owning the source fact."""
        connection.execute(
            "INSERT INTO product_events(event_id,subject,type,data,created_at) VALUES(?,?,?,?,?)",
            (
                uuid4().hex,
                subject,
                kind,
                json.dumps(data, ensure_ascii=False, allow_nan=False),
                datetime.now(UTC).isoformat(),
            ),
        )

    def sequence(self, connection: sqlite3.Connection) -> int:
        """Read the committed event high watermark in the current snapshot."""
        return int(connection.execute("SELECT COALESCE(MAX(sequence),0) FROM product_events").fetchone()[0])

    def get_space(self, space_id: str) -> Space | None:
        """Read a Space, retaining tombstones for reference reconciliation."""
        with self.transaction() as connection:
            return self.load(connection, "spaces", space_id, Space)

    def get_execution(self, execution_id: str) -> Execution | None:
        """Read one durable job without starting or resuming its Agent."""
        with self.transaction() as connection:
            return self.load(connection, "executions", execution_id, Execution)

    def get_attention(self, attention_id: str) -> Attention | None:
        """Read pending or resolved user-decision facts by stable identity."""
        with self.transaction() as connection:
            return self.load(connection, "attentions", attention_id, Attention)

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        """Read artifact metadata without exposing an absolute file path."""
        with self.transaction() as connection:
            return self.load(connection, "artifacts", artifact_id, Artifact)

    def get_attachment(self, attachment_id: str) -> Attachment | None:
        """Read validated uploaded-content metadata."""
        with self.transaction() as connection:
            return self.load(connection, "attachments", attachment_id, Attachment)

    def get_input(self, input_id: str) -> InboxItem | None:
        """Read a stable input payload for SDK delivery or source inspection."""
        with self.transaction() as connection:
            return self.load(connection, "inbox_items", input_id, InboxItem)

    def seed_execution_input(self, execution_id: str) -> InboxItem:
        """Attach one stable initial work input when no explicit dispatch input exists."""
        with self.transaction(write=True) as connection:
            execution = self.writable_execution(connection, execution_id)
            existing = tuple(
                value
                for value in self.records(connection, "inbox_items", InboxItem)
                if value.target_kind == "execution" and value.target_id == execution_id
            )
            if existing:
                return existing[0]
            origin_id = stable_id(f"execution-initial/{execution_id}")
            origin = self.enqueue(
                connection,
                input_id=origin_id,
                origin_input_id=origin_id,
                target_kind="router",
                target_id="router",
                content=execution.objective,
                source="weave.execution",
                wake_policy="silent",
                causation_id=execution_id,
            )
            return self.assign_input(connection, origin, execution_id)

    def worker_inputs(self, execution_id: str, *, pending_only: bool = True) -> tuple[InboxItem, ...]:
        """Read product input responsibilities, independently of SDK consumption receipts."""
        with self.transaction() as connection:
            self.require(connection, "executions", execution_id, Execution)
            return tuple(
                value
                for value in self.records(connection, "inbox_items", InboxItem)
                if value.target_kind == "execution"
                and value.target_id == execution_id
                and (not pending_only or value.status != "handled")
            )

    def active_space(self, connection: sqlite3.Connection, space_id: str) -> Space:
        """Require an active, non-deleting Space for accepting new work."""
        space = self.require(connection, "spaces", space_id, Space)
        if space.deleted or space.deletion_pending or space.status != "active":
            raise ProductConflict("The Space cannot accept new work", current_revision=space.revision)
        return space

    def writable_execution(self, connection: sqlite3.Connection, execution_id: str) -> Execution:
        """Require a live job whose owning Space is outside cleanup."""
        execution = self.require(connection, "executions", execution_id, Execution)
        space = self.require(connection, "spaces", execution.space_id, Space)
        if space.deleted or space.deletion_pending or execution.status in TERMINAL_STATUSES:
            raise ProductConflict("The target Execution cannot accept new input", current_revision=execution.revision)
        return execution

    def check_revision(self, current: int, expected: int) -> None:
        """Reject a mutation based on an obsolete product snapshot."""
        if current != expected:
            raise ProductConflict("The product revision changed", current_revision=current)

    def save_space(self, connection: sqlite3.Connection, value: Space) -> None:
        """Save a validated Space with its indexed revision and status."""
        self.save(connection, "spaces", value.space_id, value, revision=value.revision, status=value.status)

    def save_execution(self, connection: sqlite3.Connection, value: Execution) -> None:
        """Save validated execution ownership and its exact product state."""
        self.save(
            connection,
            "executions",
            value.execution_id,
            value,
            space_id=value.space_id,
            parent_id=value.parent_execution_id,
            session_id=value.session_id,
            status=value.status.value,
            revision=value.revision,
        )

    def create_space(self, configuration: SpaceConfiguration, *, request_id: str, space_id: str | None = None) -> Space:
        """Create one revisioned Worker identity idempotently."""
        digest = fingerprint(configuration)
        with self.transaction(write=True) as connection:
            old = self.receipt(connection, f"space/create/{request_id}", digest, Space)
            if old is not None:
                return old
            result = Space(
                **configuration.model_dump(),
                space_id=stable_id(f"space/{request_id}") if space_id is None else space_id,
            )
            if self.load(connection, "spaces", result.space_id, Space) is not None:
                raise ProductConflict("Space identity already exists")
            self.save_space(connection, result)
            self.emit(connection, result.space_id, "space.created", result.model_dump(mode="json"))
            self.save_receipt(connection, f"space/create/{request_id}", digest, result)
            return result

    def update_space(
        self, space_id: str, configuration: SpaceConfiguration, *, request_id: str, expected_revision: int
    ) -> Space:
        """Replace the observed configuration without mutating existing snapshots."""
        digest = fingerprint(
            {
                "space_id": space_id,
                "configuration": configuration.model_dump(mode="json"),
                "expected_revision": expected_revision,
            }
        )
        with self.transaction(write=True) as connection:
            old = self.receipt(connection, f"space/update/{request_id}", digest, Space)
            if old is not None:
                return old
            current = self.require(connection, "spaces", space_id, Space)
            self.check_revision(current.revision, expected_revision)
            if current.deletion_pending or current.deleted:
                raise ProductConflict("The Space is being deleted")
            value = Space(
                **configuration.model_dump(),
                space_id=space_id,
                revision=current.revision + 1,
                status=current.status,
                created_at=current.created_at,
            )
            self.save_space(connection, value)
            self.emit(connection, space_id, "space.updated", value.model_dump(mode="json"))
            self.save_receipt(connection, f"space/update/{request_id}", digest, value)
            return value

    def archive_space(self, space_id: str, *, request_id: str, expected_revision: int, restore: bool = False) -> Space:
        """Change new-work admission while leaving already accepted work owned."""
        digest = fingerprint({"space_id": space_id, "expected_revision": expected_revision, "restore": restore})
        with self.transaction(write=True) as connection:
            old = self.receipt(connection, f"space/archive/{request_id}", digest, Space)
            if old is not None:
                return old
            current = self.require(connection, "spaces", space_id, Space)
            self.check_revision(current.revision, expected_revision)
            if current.deletion_pending or current.deleted:
                raise ProductConflict("The Space is being deleted")
            value = current.model_copy(
                update={"status": "active" if restore else "archived", "revision": current.revision + 1}
            )
            self.save_space(connection, value)
            self.emit(
                connection, space_id, "space.restored" if restore else "space.archived", value.model_dump(mode="json")
            )
            self.save_receipt(connection, f"space/archive/{request_id}", digest, value)
            return value

    def insert_execution(self, connection: sqlite3.Connection, spec: ExecutionSpec, execution_id: str) -> Execution:
        """Create actual work with an independent Session and frozen Space revision."""
        space = self.active_space(connection, spec.space_id)
        if self.load(connection, "executions", execution_id, Execution) is not None:
            raise ProductConflict("Execution identity already exists")
        if spec.parent_execution_id is not None:
            self.writable_execution(connection, spec.parent_execution_id)
        if spec.source_execution_id is not None:
            source = self.require(connection, "executions", spec.source_execution_id, Execution)
            if source.status not in TERMINAL_STATUSES or spec.parent_execution_id is not None:
                raise ProductConflict("Retry requires a terminal source and a new root Execution")
        value = Execution(
            **spec.model_dump(),
            execution_id=execution_id,
            session_id=stable_id(f"session/{execution_id}"),
            config_snapshot=space,
        )
        self.save_execution(connection, value)
        if spec.parent_execution_id is not None:
            self.reserve_budget_in_transaction(connection, spec.parent_execution_id, execution_id, spec.budget_limit)
        self.emit(connection, execution_id, "execution.queued", value.model_dump(mode="json"))
        return value

    def create_execution(self, spec: ExecutionSpec, *, request_id: str) -> Execution:
        """Accept one independent root or delegated child idempotently."""
        digest = fingerprint(spec)
        with self.transaction(write=True) as connection:
            old = self.receipt(connection, f"execution/create/{request_id}", digest, Execution)
            if old is not None:
                return old
            value = self.insert_execution(connection, spec, stable_id(f"execution/{request_id}"))
            self.save_receipt(connection, f"execution/create/{request_id}", digest, value)
            return value

    def insert_message(
        self,
        connection: sqlite3.Connection,
        *,
        source_key: str,
        role: Literal["user", "assistant", "system"],
        body: str,
        attachment_ids: tuple[str, ...] = (),
        target_execution_ids: tuple[str, ...] = (),
        context_refs: tuple[ContextReference, ...] = (),
        reply_to_message_id: str | None = None,
        report_ids: tuple[str, ...] = (),
        source_message_ids: tuple[str, ...] = (),
    ) -> Message:
        """Commit a formal chat entry and all of its already validated references."""
        message_id = stable_id(f"message/{source_key}")
        cursor = connection.execute(
            "INSERT INTO chat_messages(id,source_key,payload) VALUES(?,?,?)", (message_id, source_key, "{}")
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Message insertion did not allocate a chat sequence")
        value = Message(
            message_id=message_id,
            chat_sequence=cursor.lastrowid,
            role=role,
            body=body,
            source_key=source_key,
            attachment_ids=attachment_ids,
            target_execution_ids=target_execution_ids,
            context_refs=context_refs,
            reply_to_message_id=reply_to_message_id,
            report_ids=report_ids,
            source_message_ids=source_message_ids,
        )
        connection.execute("UPDATE chat_messages SET payload=? WHERE id=?", (value.model_dump_json(), message_id))
        for attachment_id in attachment_ids:
            attachment = self.require(connection, "attachments", attachment_id, Attachment)
            self.save(
                connection,
                "attachments",
                attachment_id,
                attachment.model_copy(update={"status": "referenced", "expires_at": None}),
                client_id=attachment.client_attachment_id,
                digest=attachment.digest,
            )
            self.add_reference(connection, "message", message_id, "attachment", attachment_id)
        for reference in context_refs:
            self.add_reference(connection, "message", message_id, reference.kind, reference.id)
            if reference.kind == "graph_node":
                row = connection.execute("SELECT payload FROM node_attempts WHERE id=?", (reference.id,)).fetchone()
                if row is None:
                    raise LookupError("The referenced node attempt does not exist")
                attempt = JSON_OBJECT.validate_json(row["payload"])
                for artifact_id in ARTIFACT_IDS.validate_python(attempt["artifact_ids"]):
                    self.add_reference(connection, "message", message_id, "artifact", artifact_id)
        for target in target_execution_ids:
            self.add_reference(connection, "message", message_id, "execution", target)
        self.emit(
            connection,
            message_id,
            "chat.message.created",
            self.project_message(connection, value).model_dump(mode="json"),
        )
        return value

    def add_reference(
        self, connection: sqlite3.Connection, owner_kind: str, owner_id: str, target_kind: str, target_id: str
    ) -> None:
        """Record ownership needed to reject cleanup races and preserve shared content."""
        connection.execute(
            "INSERT OR IGNORE INTO object_references VALUES(?,?,?,?)", (owner_kind, owner_id, target_kind, target_id)
        )

    def validate_references(
        self,
        connection: sqlite3.Connection,
        attachments: tuple[str, ...],
        references: tuple[ContextReference, ...],
        reply_to: str | None = None,
    ) -> None:
        """Validate all user references atomically before any message is accepted."""
        total = 0
        for identity in attachments:
            attachment = self.require(connection, "attachments", identity, Attachment)
            if attachment.status == "deleted" or (
                attachment.expires_at is not None and attachment.expires_at <= datetime.now(UTC)
            ):
                raise ProductConflict("The attachment is unavailable or expired")
            total += attachment.size
        if total > 100 * 1024 * 1024:
            raise ProductCapacityError("Attachments exceed the message's 100 MiB limit")
        if (
            reply_to is not None
            and connection.execute("SELECT 1 FROM chat_messages WHERE id=?", (reply_to,)).fetchone() is None
        ):
            raise LookupError("The referenced chat message does not exist")
        for reference in references:
            if reference.kind == "space":
                space = self.require(connection, "spaces", reference.id, Space)
                if space.deletion_pending or space.deleted:
                    raise ProductConflict("The referenced Space is being deleted")
            elif reference.kind == "artifact":
                artifact = self.require(connection, "artifacts", reference.id, Artifact)
                if artifact.deletion_state != "active":
                    raise ProductConflict("The referenced artifact is unavailable")
                owner = self.require(connection, "executions", artifact.owner_execution_id, Execution)
                space = self.require(connection, "spaces", owner.space_id, Space)
                if space.deletion_pending or space.deleted:
                    raise ProductConflict("The artifact's Space is being deleted")
            else:
                execution_id = reference.id if reference.kind == "execution" else reference.execution_id
                if execution_id is None:
                    raise ValueError("Missing reference Execution")
                execution = self.require(connection, "executions", execution_id, Execution)
                space = self.require(connection, "spaces", execution.space_id, Space)
                if space.deletion_pending or space.deleted:
                    raise ProductConflict("The referenced Execution's Space is being deleted")
                if reference.kind == "graph_node":
                    row = connection.execute("SELECT payload FROM node_attempts WHERE id=?", (reference.id,)).fetchone()
                    if row is None:
                        raise LookupError("The graph node does not belong to this Execution")
                    attempt = JSON_OBJECT.validate_json(row["payload"])
                    if (
                        attempt.get("execution_id") != execution_id
                        or attempt.get("graph_run_id") != reference.graph_run_id
                        or attempt.get("node_id") != reference.node_id
                        or (reference.node_attempt_id is not None and reference.node_attempt_id != reference.id)
                    ):
                        raise ProductConflict("The graph node reference does not match its actual attempt")

    def outcome_admission(
        self,
        connection: sqlite3.Connection,
        *,
        source: str,
        input_id: str,
        origin_input_id: str,
        target_kind: str,
        target_id: str,
        causation_id: str | None,
    ) -> bool:
        """Reserve progress for exact outcomes already owed by durable host producers."""
        if source == "weave.report":
            report = self.load(connection, "reports", causation_id, Report) if causation_id is not None else None
            valid = (
                report is not None
                and report.kind != "progress"
                and not report.rejection_reasons
                and target_kind == "router"
                and target_id == "router"
                and input_id == origin_input_id == stable_id(f"report-input/{causation_id}")
            )
        elif source == "weave.child":
            child = self.load(connection, "executions", causation_id, Execution) if causation_id is not None else None
            valid = (
                child is not None
                and child.status in TERMINAL_STATUSES
                and child.parent_execution_id == target_id
                and target_kind == "execution"
                and input_id == origin_input_id == stable_id(f"child-result/{child.execution_id}/{child.revision}")
            )
        elif source == "weave.validation":
            report = self.load(connection, "reports", causation_id, Report) if causation_id is not None else None
            valid = (
                report is not None
                and bool(report.rejection_reasons)
                and report.execution_id == target_id
                and target_kind == "execution"
                and input_id == origin_input_id == stable_id(f"completion-correction/{causation_id}")
            )
            if any(
                item.target_kind == "execution"
                and item.target_id == target_id
                and item.source == "weave.validation"
                and item.status != "handled"
                for item in self.records(connection, "inbox_items", InboxItem)
            ):
                raise ProductConflict("The Execution already owns a pending completion correction")
        elif source == "weave.handback":
            origin = self.load(connection, "inbox_items", causation_id, InboxItem) if causation_id is not None else None
            dispositions = tuple(
                value
                for value in self.records(connection, "input_dispositions", InputDisposition)
                if value.origin_input_id == causation_id
            )
            valid = (
                origin is not None
                and bool(dispositions)
                and all(value.state != "owned" for value in dispositions)
                and any(
                    value.origin_input_id == causation_id
                    for value in self.records(connection, "input_handbacks", Handback)
                )
                and target_kind == "router"
                and target_id == "router"
                and origin_input_id == causation_id
                and input_id == f"{causation_id}/{origin.reroute_round}/handback_ready"
            )
        elif source == "weave.execution":
            execution = (
                self.load(connection, "executions", causation_id, Execution) if causation_id is not None else None
            )
            original = stable_id(f"execution-initial/{causation_id}")
            valid = (
                execution is not None
                and origin_input_id == original
                and (
                    (target_kind == "router" and target_id == "router" and input_id == original)
                    or (
                        target_kind == "execution"
                        and target_id == execution.execution_id
                        and input_id == stable_id(f"input/{original}/{target_id}")
                    )
                )
            )
        elif source == "weave.attention":
            row = connection.execute("SELECT payload FROM chat_messages WHERE id=?", (causation_id,)).fetchone()
            answer = None if row is None else Message.model_validate_json(row["payload"])
            attention = next(
                (
                    value
                    for value in self.records(connection, "attentions", Attention)
                    if value.status == "resolved" and value.resolution_source == causation_id
                ),
                None,
            )
            reply_origin = None if attention is None else attention.resolution_input_id
            routed = self.load(connection, "inbox_items", origin_input_id, InboxItem)
            routed_answer = (
                routed is not None
                and routed.input_id == reply_origin
                and routed.origin_input_id == reply_origin
                and routed.target_kind == "router"
                and routed.target_id == "router"
                and routed.status == "claimed"
                and routed.source == source
                and routed.causation_id == causation_id
            )
            valid = (
                attention is not None
                and answer is not None
                and answer.role == "user"
                and reply_origin is not None
                and origin_input_id == reply_origin
                and (
                    (target_kind == "router" and target_id == "router" and input_id == reply_origin)
                    or (
                        target_kind == "execution"
                        and (target_id == attention.execution_id or (attention.execution_id is None and routed_answer))
                        and input_id == stable_id(f"input/{reply_origin}/{target_id}")
                    )
                )
            )
        elif source == "weave.control":
            row = connection.execute("SELECT payload FROM chat_messages WHERE id=?", (causation_id,)).fetchone()
            activity = None if row is None else Message.model_validate_json(row["payload"])
            valid = (
                activity is not None
                and activity.source_key.startswith("control/")
                and activity.role == "system"
                and bool(activity.target_execution_ids)
                and target_kind == "router"
                and target_id == "router"
                and input_id == origin_input_id == stable_id(activity.source_key)
            )
        elif source == "weave.reassessment":
            attempts = tuple(
                value
                for value in self.records(connection, "route_attempts", RouteAttempt)
                if value.decision_id == causation_id
            )
            current = max(attempts, key=lambda value: value.attempt) if attempts else None
            valid = (
                current is not None
                and current.attempt < 3
                and target_kind == "router"
                and target_id == "router"
                and input_id == origin_input_id == f"{causation_id}/{current.attempt + 1}/reassessment"
            )
        else:
            return False
        if not valid:
            raise ProductConflict("The host outcome does not match its durable producer identity")
        return True

    def enqueue(
        self,
        connection: sqlite3.Connection,
        *,
        input_id: str,
        origin_input_id: str,
        target_kind: Literal["router", "execution"],
        target_id: str,
        content: str,
        source: str,
        wake_policy: Literal["wake", "silent"],
        kind: Literal["user", "observation", "delegate_result", "operation_result"] = "observation",
        causation_id: str | None = None,
    ) -> InboxItem:
        """Persist immutable input and SDK delivery intent in the same transaction."""
        old = self.load(connection, "inbox_items", input_id, InboxItem)
        if old is not None:
            if (
                old.origin_input_id,
                old.target_kind,
                old.target_id,
                old.content,
                old.source,
                old.kind,
                old.causation_id,
                old.wake_policy,
            ) != (origin_input_id, target_kind, target_id, content, source, kind, causation_id, wake_policy):
                raise ProductConflict("The input identity has conflicting content")
            return old
        count = connection.execute(
            "SELECT COUNT(*) FROM inbox_items WHERE target_kind=? AND target_id=? AND status!='handled'",
            (target_kind, target_id),
        ).fetchone()[0]
        reserved = self.outcome_admission(
            connection,
            source=source,
            input_id=input_id,
            origin_input_id=origin_input_id,
            target_kind=target_kind,
            target_id=target_id,
            causation_id=causation_id,
        )
        if count >= self.queue_capacity and not reserved:
            raise ProductCapacityError("The input target has reached its queue capacity")
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM inbox_items WHERE target_kind=? AND target_id=?",
            (target_kind, target_id),
        ).fetchone()[0]
        value = InboxItem(
            input_id=input_id,
            origin_input_id=origin_input_id,
            target_kind=target_kind,
            target_id=target_id,
            target_sequence=sequence,
            kind=kind,
            source=source,
            content=content,
            causation_id=causation_id,
            wake_policy=wake_policy,
        )
        self.save_input(connection, value)
        delivery = Delivery(
            delivery_id=stable_id(f"delivery/{input_id}"),
            input_id=input_id,
            target_kind=target_kind,
            target_id=target_id,
        )
        self.save(
            connection,
            "deliveries",
            delivery.delivery_id,
            delivery,
            input_id=input_id,
            state=delivery.state,
            generation=delivery.generation,
        )
        return value

    def save_input(self, connection: sqlite3.Connection, value: InboxItem) -> None:
        """Save input routing facts while preserving its immutable source payload."""
        self.save(
            connection,
            "inbox_items",
            value.input_id,
            value,
            origin_id=value.origin_input_id,
            target_kind=value.target_kind,
            target_id=value.target_id,
            sequence=value.target_sequence,
            status=value.status,
            wake=value.wake_policy,
        )

    def assign_input(self, connection: sqlite3.Connection, origin: InboxItem, execution_id: str) -> InboxItem:
        """Create one unique target responsibility for an original input."""
        self.writable_execution(connection, execution_id)
        key = stable_id(f"disposition/{origin.origin_input_id}/{execution_id}")
        if self.load(connection, "input_dispositions", key, InputDisposition) is not None:
            raise ProductConflict("This Execution already owned the original input")
        value = self.enqueue(
            connection,
            input_id=stable_id(f"input/{origin.origin_input_id}/{execution_id}"),
            origin_input_id=origin.origin_input_id,
            target_kind="execution",
            target_id=execution_id,
            content=origin.content,
            source=origin.source,
            wake_policy="wake",
            kind=origin.kind,
            causation_id=origin.causation_id,
        )
        disposition = InputDisposition(
            origin_input_id=origin.origin_input_id, target_execution_id=execution_id, input_id=value.input_id
        )
        self.save_disposition(connection, disposition)
        if (
            origin.causation_id is not None
            and connection.execute("SELECT 1 FROM chat_messages WHERE id=?", (origin.causation_id,)).fetchone()
            is not None
        ):
            self.add_reference(connection, "message", origin.causation_id, "execution", execution_id)
        return value

    def save_disposition(self, connection: sqlite3.Connection, value: InputDisposition) -> None:
        """Save one target's responsibility and unique original-input relation."""
        self.save(
            connection,
            "input_dispositions",
            stable_id(f"disposition/{value.origin_input_id}/{value.target_execution_id}"),
            value,
            origin_id=value.origin_input_id,
            target_id=value.target_execution_id,
            input_id=value.input_id,
            state=value.state,
        )

    def submit_message(self, request: ChatMessageRequest) -> MessageReceipt:
        """Accept every target or reject the entire message without losing its retry key."""
        digest = fingerprint(request)
        with self.transaction(write=True) as connection:
            old = self.receipt(connection, f"message/{request.client_message_id}", digest, MessageReceipt)
            if old is not None:
                return old
            self.validate_references(
                connection, request.attachment_ids, request.context_refs, request.reply_to_message_id
            )
            for execution_id in request.target_execution_ids:
                self.writable_execution(connection, execution_id)
            message = self.insert_message(
                connection,
                source_key=f"client/{request.client_message_id}",
                role="user",
                body=request.text,
                attachment_ids=request.attachment_ids,
                target_execution_ids=request.target_execution_ids,
                context_refs=request.context_refs,
                reply_to_message_id=request.reply_to_message_id,
            )
            origin_id = stable_id(f"origin/{message.message_id}")
            origin = self.enqueue(
                connection,
                input_id=origin_id,
                origin_input_id=origin_id,
                target_kind="router",
                target_id="router",
                content=request.text,
                source="weave.message",
                kind="user",
                wake_policy="silent" if request.target_execution_ids else "wake",
                causation_id=message.message_id,
            )
            inputs = tuple(
                self.assign_input(connection, origin, target).input_id for target in request.target_execution_ids
            )
            result = MessageReceipt(
                message_id=message.message_id,
                sequence=message.chat_sequence,
                input_ids=inputs if inputs else (origin_id,),
            )
            self.save_receipt(connection, f"message/{request.client_message_id}", digest, result)
            return result

    def record_execution_run(self, value: ExecutionRun) -> ExecutionRun:
        """Persist the invocation before SDK startup and associate later receipts safely."""
        identity = f"{value.execution_id}/{value.attempt}"
        with self.transaction(write=True) as connection:
            execution = self.require(connection, "executions", value.execution_id, Execution)
            current = self.load(connection, "execution_runs", identity, ExecutionRun)
            if current is not None and current == value:
                return current
            if current is not None:
                if current.invocation_id != value.invocation_id or (
                    current.run_id is not None and current.run_id != value.run_id
                ):
                    raise ProductConflict("The execution attempt already has another SDK invocation")
                if current.state in {"succeeded", "failed", "cancelled"} and current != value:
                    raise ProductConflict("A terminal SDK receipt is immutable")
            if execution.status in TERMINAL_STATUSES and current != value:
                raise ProductConflict("Cannot start another Run for a terminal Execution")
            self.save(
                connection,
                "execution_runs",
                identity,
                value,
                execution_id=value.execution_id,
                invocation_id=value.invocation_id,
            )
            self.save_execution(
                connection,
                execution.model_copy(
                    update={
                        "run_attempt": max(execution.run_attempt, value.attempt),
                        "current_run_id": value.run_id,
                        "checkpoint_ref": value.checkpoint_ref,
                        "revision": execution.revision + 1,
                    }
                ),
            )
            return value

    def recover_unstarted_run(self, execution_id: str, invocation_id: str) -> Execution:
        """Requeue an invocation after its owner confirmed the SDK never accepted it.

        The service must hold exclusive workspace ownership and query the same SDK
        Session by this invocation ID before calling this transaction.
        """
        with self.transaction(write=True) as connection:
            execution = self.writable_execution(connection, execution_id)
            if execution.current_run_id is not None:
                raise ProductConflict("An accepted SDK Run cannot be requeued as an unstarted invocation")
            mapping = self.load(connection, "execution_runs", f"{execution_id}/{execution.run_attempt}", ExecutionRun)
            if mapping is None:
                if execution.run_attempt != 0 or invocation_id != f"{execution_id}/1":
                    raise ProductConflict("The unstarted execution has a different invocation identity")
            elif mapping.invocation_id != invocation_id or mapping.run_id is not None or mapping.state != "pending":
                raise ProductConflict("Only the exact unaccepted SDK invocation can be requeued")
            target = self.resumable_state(connection, execution)
            value = execution.model_copy(update={"status": target, "revision": execution.revision + 1})
            self.save_execution(connection, value)
            self.emit(connection, value.execution_id, "execution.updated", value.model_dump(mode="json"))
            return value

    def project_pending_attentions(
        self,
        execution_id: str,
        run_id: str,
        *,
        attentions: tuple[Attention, ...],
        has_interruptions: bool,
    ) -> Execution:
        """Publish a safe checkpoint's complete pending decision set in one transaction."""
        identities = {value.attention_id for value in attentions}
        if len(identities) != len(attentions):
            raise ValueError("Pending user requests require unique SDK interruption identities")
        with self.transaction(write=True) as connection:
            execution = self.writable_execution(connection, execution_id)
            mapping = self.require(
                connection, "execution_runs", f"{execution_id}/{execution.run_attempt}", ExecutionRun
            )
            if execution.current_run_id != run_id or mapping.run_id != run_id or mapping.state != "paused":
                raise ProductConflict("The paused checkpoint must belong to the current invocation")
            for value in attentions:
                if value.execution_id != execution_id or value.checkpoint_ref != run_id:
                    raise ProductConflict("The user request belongs to another checkpoint owner")
                old = self.load(connection, "attentions", value.attention_id, Attention)
                if old is not None:
                    immutable_fields = (
                        "execution_id",
                        "kind",
                        "question",
                        "options",
                        "action",
                        "action_digest",
                        "checkpoint_ref",
                    )
                    if any(getattr(old, field) != getattr(value, field) for field in immutable_fields):
                        raise ProductConflict("The SDK interruption identity has different request content")
                    continue
                self.save_attention(connection, value)
                self.emit(connection, value.attention_id, "attention.created", value.model_dump(mode="json"))
            for old in self.records(connection, "attentions", Attention):
                if (
                    old.execution_id == execution_id
                    and old.checkpoint_ref == run_id
                    and old.kind in {"approval", "clarification"}
                    and old.status == "pending"
                    and old.attention_id not in identities
                ):
                    withdrawn = old.model_copy(update={"status": "withdrawn", "revision": old.revision + 1})
                    self.save_attention(connection, withdrawn)
                    self.emit(connection, old.attention_id, "attention.withdrawn", withdrawn.model_dump(mode="json"))
            target = self.resumable_state(connection, execution)
            if target is ExecutionStatus.QUEUED and not has_interruptions:
                target = ExecutionStatus.PAUSED
            projected = execution.model_copy(
                update={
                    "status": target,
                    "checkpoint_ref": run_id,
                    "reason": execution.reason if target is ExecutionStatus.BLOCKED else None,
                    "revision": execution.revision + 1,
                }
            )
            self.save_execution(connection, projected)
            self.emit(connection, projected.execution_id, "execution.updated", projected.model_dump(mode="json"))
            return projected

    def recover_finished_run(self, execution_id: str, run_id: str) -> Execution:
        """Restore the projection owner for a trusted persisted terminal SDK receipt.

        The service must read the actual SDK receipt before calling this method.
        Ordinary user controls cannot use this owner-only recovery operation.
        """
        with self.transaction(write=True) as connection:
            execution = self.writable_execution(connection, execution_id)
            mapping = self.require(
                connection, "execution_runs", f"{execution_id}/{execution.run_attempt}", ExecutionRun
            )
            if execution.current_run_id != run_id or mapping.run_id != run_id:
                raise ProductConflict("The recovered SDK receipt must belong to the current invocation")
            target = ExecutionStatus.CANCELLING if execution.control_intent == "cancel" else ExecutionStatus.RUNNING
            if execution.status is target:
                return execution
            value = execution.model_copy(update={"status": target, "revision": execution.revision + 1})
            self.save_execution(connection, value)
            self.emit(connection, value.execution_id, "execution.updated", value.model_dump(mode="json"))
            return value

    def withdraw_attentions(self, execution_id: str, *, kind: str | None = None) -> None:
        """Withdraw pending requests whose owning runtime condition has been settled."""
        with self.transaction(write=True) as connection:
            self.require(connection, "executions", execution_id, Execution)
            for current in self.records(connection, "attentions", Attention):
                if (
                    current.execution_id == execution_id
                    and current.status == "pending"
                    and (kind is None or current.kind == kind)
                ):
                    value = current.model_copy(update={"status": "withdrawn", "revision": current.revision + 1})
                    self.save_attention(connection, value)
                    self.emit(connection, value.attention_id, "attention.withdrawn", value.model_dump(mode="json"))

    def execution_runs(self, execution_id: str) -> tuple[ExecutionRun, ...]:
        """Read stable SDK invocation mappings owned by one Execution."""
        with self.transaction() as connection:
            self.require(connection, "executions", execution_id, Execution)
            return tuple(
                value
                for value in self.records(connection, "execution_runs", ExecutionRun)
                if value.execution_id == execution_id
            )

    def execution_run_page(
        self, execution_id: str, *, before_sequence: int | None = None, limit: int = 50
    ) -> ExecutionRunPage:
        """Read newest invocation attempts and their watermark in one transaction."""
        if limit < 1 or limit > 100 or (before_sequence is not None and before_sequence < 1):
            raise ValueError("Invalid Run page boundary or limit")
        with self.transaction() as connection:
            self.require(connection, "executions", execution_id, Execution)
            records = sorted(
                (
                    value
                    for value in self.records(connection, "execution_runs", ExecutionRun)
                    if value.execution_id == execution_id
                ),
                key=lambda value: value.attempt,
                reverse=True,
            )
            selected = [value for value in records if before_sequence is None or value.attempt < before_sequence]
            items = tuple(selected[:limit])
            return ExecutionRunPage(
                items=items,
                total=len(records),
                next_before_sequence=items[-1].attempt if len(selected) > limit else None,
                latest_sequence=records[0].attempt if records else None,
                latest_run_id=records[0].run_id if records else None,
                read_sequence=self.sequence(connection),
            )

    def change_state(
        self,
        connection: sqlite3.Connection,
        execution: Execution,
        status: ExecutionStatus,
        *,
        reason: str | None = None,
        result: str | None = None,
        control_intent: Literal["none", "pause", "cancel"] | None = None,
        checkpoint_ref: str | None = None,
        unknown_effects: tuple[str, ...] | None = None,
    ) -> Execution:
        """Validate and commit the single product state transition contract."""
        if status != execution.status and status not in TRANSITIONS[execution.status]:
            raise ProductConflict(
                f"Execution transition {execution.status} -> {status} is invalid", current_revision=execution.revision
            )
        if execution.status in TERMINAL_STATUSES:
            raise ProductConflict("A terminal Execution is immutable", current_revision=execution.revision)
        if status is ExecutionStatus.BLOCKED and reason is None:
            raise ValueError("Blocked work requires an explicit recovery reason")
        value = Execution.model_validate(
            {
                **execution.model_dump(),
                "status": status,
                "reason": reason,
                "result": execution.result if result is None else result,
                "control_intent": execution.control_intent if control_intent is None else control_intent,
                "checkpoint_ref": execution.checkpoint_ref if checkpoint_ref is None else checkpoint_ref,
                "unknown_effects": execution.unknown_effects if unknown_effects is None else unknown_effects,
                "revision": execution.revision + 1,
            }
        )
        if status is ExecutionStatus.CANCELLED and value.unknown_effects:
            raise ProductConflict("Unknown external effects prevent confirmed cancellation")
        if status is ExecutionStatus.CANCELLED and any(
            child.parent_execution_id == execution.execution_id and child.status not in TERMINAL_STATUSES
            for child in self.records(connection, "executions", Execution)
        ):
            raise ProductConflict("Cancellation must wait for owned child cleanup")
        if status in {ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
            for disposition in self.records(connection, "input_dispositions", InputDisposition):
                if disposition.target_execution_id == execution.execution_id and disposition.state == "owned":
                    self.return_in_transaction(
                        connection,
                        disposition.input_id,
                        execution_id=execution.execution_id,
                        expected_revision=disposition.revision,
                        reason=f"Execution {status} before resolving this accepted input",
                        effect_refs=value.unknown_effects,
                        unknown_effects=bool(value.unknown_effects),
                    )
        self.save_execution(connection, value)
        self.emit(connection, value.execution_id, "execution.updated", value.model_dump(mode="json"))
        if status in TERMINAL_STATUSES:
            self.settle_child_in_transaction(connection, value)
        return value

    def transition_execution(
        self,
        execution_id: str,
        status: ExecutionStatus,
        *,
        expected_revision: int,
        reason: str | None = None,
        result: str | None = None,
        checkpoint_ref: str | None = None,
        unknown_effects: tuple[str, ...] | None = None,
    ) -> Execution:
        """Apply host-confirmed runtime facts after checking the observed revision."""
        with self.transaction(write=True) as connection:
            execution = self.require(connection, "executions", execution_id, Execution)
            self.check_revision(execution.revision, expected_revision)
            if status is ExecutionStatus.SUCCEEDED:
                raise ValueError("Successful completion requires an accepted ready Report")
            if (
                status is ExecutionStatus.PAUSED
                and execution.status is ExecutionStatus.RUNNING
                and checkpoint_ref is None
            ):
                raise ProductConflict("Running work requires a safe checkpoint before becoming paused")
            return self.change_state(
                connection,
                execution,
                status,
                reason=reason,
                result=result,
                checkpoint_ref=checkpoint_ref,
                unknown_effects=unknown_effects,
            )

    def prerequisites(self, connection: sqlite3.Connection, execution: Execution) -> tuple[str, ...]:
        """Derive outstanding waits from independent persisted decision and child facts."""
        pending = tuple(
            value
            for value in self.records(connection, "attentions", Attention)
            if value.execution_id == execution.execution_id and value.status == "pending"
        )
        reasons: list[str] = []
        if any(value.kind == "recovery" for value in pending):
            reasons.append("recovery")
        if any(value.kind == "approval" for value in pending):
            reasons.append("approval")
        if any(value.kind in {"clarification", "review"} for value in pending):
            reasons.append("input")
        if any(
            value.parent_execution_id == execution.execution_id
            and value.required
            and value.status not in TERMINAL_STATUSES
            for value in self.records(connection, "executions", Execution)
        ):
            reasons.append("children")
        return tuple(reasons)

    def resumable_state(self, connection: sqlite3.Connection, execution: Execution) -> ExecutionStatus:
        """Honor controls first and then reduce every remaining waiting condition."""
        if execution.unknown_effects:
            return ExecutionStatus.BLOCKED
        if execution.control_intent == "cancel":
            return ExecutionStatus.CANCELLING
        if execution.control_intent == "pause":
            return ExecutionStatus.PAUSED
        waits = self.prerequisites(connection, execution)
        if "recovery" in waits:
            return ExecutionStatus.BLOCKED
        for wait, status in (
            ("approval", ExecutionStatus.WAITING_APPROVAL),
            ("input", ExecutionStatus.WAITING_INPUT),
            ("children", ExecutionStatus.WAITING_CHILDREN),
        ):
            if wait in waits:
                return status
        return ExecutionStatus.QUEUED

    def control_execution(
        self,
        execution_id: str,
        action: Literal["pause", "cancel", "resume"],
        *,
        request_id: str,
        expected_revision: int,
    ) -> Execution:
        """Persist direct user controls independently of Router model progress."""
        digest = fingerprint({"execution_id": execution_id, "action": action, "expected_revision": expected_revision})
        with self.transaction(write=True) as connection:
            old = self.receipt(connection, f"control/{request_id}", digest, Execution)
            if old is not None:
                return old
            current = self.require(connection, "executions", execution_id, Execution)
            self.check_revision(current.revision, expected_revision)
            if current.status in TERMINAL_STATUSES:
                raise ProductConflict("The Execution is terminal")
            if action == "resume":
                if current.status not in {ExecutionStatus.PAUSED, ExecutionStatus.BLOCKED}:
                    raise ProductConflict("Only paused or blocked work can be resumed")
                intent: Literal["none", "pause", "cancel"] = (
                    "none" if current.status is ExecutionStatus.PAUSED else current.control_intent
                )
                value = current.model_copy(update={"control_intent": intent})
                status = self.resumable_state(connection, value)
            elif action == "pause":
                intent = "pause"
                status = (
                    current.status
                    if current.status in {ExecutionStatus.RUNNING, ExecutionStatus.BLOCKED}
                    else ExecutionStatus.PAUSED
                )
            else:
                intent = "cancel"
                status = ExecutionStatus.CANCELLING
            value = self.change_state(
                connection,
                current,
                status,
                reason="recovery_unknown" if status is ExecutionStatus.BLOCKED else None,
                control_intent=intent,
            )
            if action == "cancel":
                self.cancel_children(connection, execution_id)
            activity = self.insert_message(
                connection,
                source_key=f"control/{request_id}",
                role="system",
                body=f"{action} requested",
                target_execution_ids=(execution_id,),
            )
            self.enqueue(
                connection,
                input_id=stable_id(f"control/{request_id}"),
                origin_input_id=stable_id(f"control/{request_id}"),
                target_kind="router",
                target_id="router",
                content=f"{action} requested for {execution_id}",
                source="weave.control",
                wake_policy="silent",
                causation_id=activity.message_id,
            )
            self.save_receipt(connection, f"control/{request_id}", digest, value)
            return value

    def cancellation_elapsed(self, execution_id: str) -> float | None:
        """Read durable cancellation latency only after confirmed product cleanup completion."""
        with self.transaction() as connection:
            self.require(connection, "executions", execution_id, Execution)
            row = connection.execute(
                "SELECT MIN(CASE WHEN json_extract(data,'$.control_intent')='cancel' "
                "THEN created_at END) AS requested, "
                "MIN(CASE WHEN json_extract(data,'$.status')='cancelled' THEN created_at END) AS completed "
                "FROM product_events WHERE subject=? AND type='execution.updated'",
                (execution_id,),
            ).fetchone()
        if row["requested"] is None or row["completed"] is None:
            return None
        elapsed = datetime.fromisoformat(row["completed"]) - datetime.fromisoformat(row["requested"])
        return max(0.0, elapsed.total_seconds())

    def cancel_children(self, connection: sqlite3.Connection, execution_id: str) -> None:
        """Propagate cancellation along actual delegation ownership only."""
        for child in self.records(connection, "executions", Execution):
            if child.parent_execution_id == execution_id and child.status not in TERMINAL_STATUSES:
                self.change_state(connection, child, ExecutionStatus.CANCELLING, control_intent="cancel")
                self.cancel_children(connection, child.execution_id)

    def retry_execution(
        self,
        execution_id: str,
        *,
        request_id: str,
        expected_revision: int,
        expected_space_revision: int,
        supplement: str = "",
    ) -> Execution:
        """Create one fresh root using the exact Space revision validated by the host.

        The observed Space revision fences new admission without changing the
        logical retry identity or invalidating an already committed receipt.
        """
        digest = fingerprint(
            {"execution_id": execution_id, "expected_revision": expected_revision, "supplement": supplement}
        )
        with self.transaction(write=True) as connection:
            old = self.receipt(connection, f"retry/{request_id}", digest, Execution)
            if old is not None:
                return old
            source = self.require(connection, "executions", execution_id, Execution)
            self.check_revision(source.revision, expected_revision)
            space = self.require(connection, "spaces", source.space_id, Space)
            if space.revision != expected_space_revision:
                raise ProductConflict(
                    "The Space configuration changed after retry validation", current_revision=space.revision
                )
            if source.status not in TERMINAL_STATUSES:
                raise ProductConflict("Re-execution requires a terminal source")
            if len(supplement.encode()) > 65_536:
                raise ValueError("The retry supplement exceeds 64 KiB")
            spec = ExecutionSpec(
                space_id=source.space_id,
                objective=source.objective if not supplement else f"{source.objective}\n\n{supplement}",
                delivery_contract=source.delivery_contract,
                budget_limit=source.budget_limit,
                source_execution_id=source.execution_id,
            )
            value = self.insert_execution(connection, spec, stable_id(f"retry/{request_id}"))
            message = self.insert_message(
                connection,
                source_key=f"retry/{request_id}",
                role="system",
                body=f"Retry requested: {value.objective}",
                target_execution_ids=(value.execution_id,),
            )
            origin_id = stable_id(f"retry-input/{request_id}")
            origin = self.enqueue(
                connection,
                input_id=origin_id,
                origin_input_id=origin_id,
                target_kind="router",
                target_id="router",
                content=value.objective,
                source="weave.retry",
                wake_policy="silent",
                causation_id=message.message_id,
            )
            self.assign_input(connection, origin, value.execution_id)
            self.save_receipt(connection, f"retry/{request_id}", digest, value)
            return value

    def available_actions_in_transaction(self, connection: sqlite3.Connection, execution: Execution) -> JsonObject:
        """Derive controls from the same transaction as their underlying job facts."""
        space = self.require(connection, "spaces", execution.space_id, Space)
        available: list[str] = []
        if not space.deleted and not space.deletion_pending:
            if execution.status in TERMINAL_STATUSES:
                if space.status == "active":
                    available.append("retry")
            else:
                if execution.status is not ExecutionStatus.CANCELLING:
                    available.append("cancel")
                    if execution.control_intent != "cancel":
                        available.append("supplement")
                if execution.status in {ExecutionStatus.PAUSED, ExecutionStatus.BLOCKED}:
                    available.append("resume")
                elif execution.control_intent != "pause" and execution.status is not ExecutionStatus.CANCELLING:
                    available.append("pause")
        return JSON_OBJECT.validate_python(
            {
                "available_actions": available,
                "disabled_reasons": {
                    action: "The current state does not permit this action"
                    for action in ("pause", "cancel", "resume", "retry", "supplement")
                    if action not in available
                },
                "pending_prerequisites": list(self.prerequisites(connection, execution)),
                "pause_requested": execution.control_intent == "pause"
                and execution.status is not ExecutionStatus.PAUSED,
            }
        )

    def execution_view(self, connection: sqlite3.Connection, execution: Execution) -> ExecutionView:
        """Build one read projection while retaining raw execution persistence contracts."""
        return ExecutionView.model_validate(
            {**execution.model_dump(), **self.available_actions_in_transaction(connection, execution)}
        )

    def space_view(self, connection: sqlite3.Connection, space: Space) -> SpaceView:
        """Count actual owned work and pending decisions in the current snapshot."""
        executions = tuple(
            value for value in self.records(connection, "executions", Execution) if value.space_id == space.space_id
        )
        identities = {value.execution_id for value in executions}
        available = (
            ()
            if space.deleted or space.deletion_pending
            else ("update", "restore" if space.status == "archived" else "archive", "delete")
        )
        return SpaceView(
            **space.model_dump(),
            available_actions=available,
            disabled_reasons={
                action: "The Space state does not permit this action"
                for action in ("update", "archive", "restore", "delete")
                if action not in available
            },
            active_execution_count=sum(value.status not in TERMINAL_STATUSES for value in executions),
            pending_attention_count=sum(
                value.status == "pending" and value.execution_id in identities
                for value in self.records(connection, "attentions", Attention)
            ),
        )

    def attention_view(self, connection: sqlite3.Connection, attention: Attention) -> AttentionView:
        """Require a still-pending, unexpired decision and a live owning scope."""
        actionable = attention.status == "pending" and (
            attention.expires_at is None or attention.expires_at > datetime.now(UTC)
        )
        execution_objective = None
        space_name = None
        if attention.execution_id is not None:
            execution = self.require(connection, "executions", attention.execution_id, Execution)
            space = self.require(connection, "spaces", execution.space_id, Space)
            execution_objective = execution.objective
            space_name = space.name
            actionable = (
                actionable
                and execution.status not in TERMINAL_STATUSES
                and execution.control_intent != "cancel"
                and not space.deleted
                and not space.deletion_pending
            )
        return AttentionView(
            **attention.model_dump(),
            execution_objective=execution_objective,
            space_name=space_name,
            available_actions=("resolve",) if actionable else (),
            disabled_reasons={}
            if actionable
            else {"resolve": "The decision is resolved, expired, or its owning work cannot accept input"},
        )

    def read_attention(self, attention_id: str) -> AttentionView:
        """Read current decision facts and action eligibility atomically."""
        with self.transaction() as connection:
            return self.attention_view(connection, self.require(connection, "attentions", attention_id, Attention))

    def available_actions(self, execution_id: str) -> JsonObject:
        """Read current execution controls and revision from one product transaction."""
        with self.transaction() as connection:
            execution = self.require(connection, "executions", execution_id, Execution)
            return {**self.available_actions_in_transaction(connection, execution), "revision": execution.revision}

    def reserve_budget_in_transaction(
        self, connection: sqlite3.Connection, parent_id: str, child_id: str, amount: int
    ) -> BudgetReservation:
        """Reserve child allowance once without allowing parent and child overspending."""
        identity = stable_id(f"budget/{parent_id}/{child_id}")
        old = self.load(connection, "budget_reservations", identity, BudgetReservation)
        if old is not None:
            if old.amount != amount:
                raise ProductConflict("The child budget has conflicting allocation")
            return old
        parent = self.writable_execution(connection, parent_id)
        if parent.budget_consumed + parent.budget_reserved + amount > parent.budget_limit:
            raise ProductCapacityError("The parent has insufficient remaining budget")
        value = BudgetReservation(
            reservation_id=identity, parent_execution_id=parent_id, child_execution_id=child_id, amount=amount
        )
        self.save(connection, "budget_reservations", identity, value, parent_id=parent_id, child_id=child_id)
        self.save_execution(
            connection,
            parent.model_copy(
                update={"budget_reserved": parent.budget_reserved + amount, "revision": parent.revision + 1}
            ),
        )
        return value

    def reserve_budget(self, parent_id: str, child_id: str, amount: int) -> BudgetReservation:
        """Reserve an independently running child's product allowance."""
        if amount < 1:
            raise ValueError("A budget reservation must be positive")
        with self.transaction(write=True) as connection:
            child = self.require(connection, "executions", child_id, Execution)
            if child.parent_execution_id != parent_id:
                raise ProductConflict("Budget allocation requires actual parent ownership")
            return self.reserve_budget_in_transaction(connection, parent_id, child_id, amount)

    def settle_budget(self, reservation_id: str, consumed: int | None) -> BudgetReservation:
        """Refund confirmed unused allowance exactly once."""
        with self.transaction(write=True) as connection:
            return self.settle_budget_in_transaction(connection, reservation_id, consumed)

    def settle_budget_in_transaction(
        self, connection: sqlite3.Connection, reservation_id: str, consumed: int | None
    ) -> BudgetReservation:
        """Release the original child reservation and retain its complete actual cost.

        A provider overage can exceed the reservation. It reduces the parent's
        remaining allowance without changing either original grant.
        """
        current = self.require(connection, "budget_reservations", reservation_id, BudgetReservation)
        if current.settled:
            if current.consumed != consumed:
                raise ProductConflict("The budget settlement conflicts with its existing receipt")
            return current
        if consumed is None:
            return current
        if consumed < 0:
            raise ValueError("Budget usage must be nonnegative")
        parent = self.require(connection, "executions", current.parent_execution_id, Execution)
        value = current.model_copy(update={"consumed": consumed, "settled": True})
        self.save(
            connection,
            "budget_reservations",
            reservation_id,
            value,
            parent_id=current.parent_execution_id,
            child_id=current.child_execution_id,
        )
        self.save_execution(
            connection,
            parent.model_copy(
                update={
                    "budget_reserved": parent.budget_reserved - current.amount,
                    "budget_consumed": parent.budget_consumed + consumed,
                    "revision": parent.revision + 1,
                }
            ),
        )
        return value

    def settle_child_in_transaction(self, connection: sqlite3.Connection, child: Execution) -> None:
        """Wake a real parent after a child's committed terminal fact."""
        if child.parent_execution_id is None:
            return
        reservation = self.load(
            connection,
            "budget_reservations",
            stable_id(f"budget/{child.parent_execution_id}/{child.execution_id}"),
            BudgetReservation,
        )
        if reservation is not None:
            self.settle_budget_in_transaction(connection, reservation.reservation_id, child.budget_consumed)
        parent = self.require(connection, "executions", child.parent_execution_id, Execution)
        if parent.status in TERMINAL_STATUSES:
            return
        input_id = stable_id(f"child-result/{child.execution_id}/{child.revision}")
        self.enqueue(
            connection,
            input_id=input_id,
            origin_input_id=input_id,
            target_kind="execution",
            target_id=parent.execution_id,
            content=child.model_dump_json(),
            source="weave.child",
            wake_policy="wake",
            kind="delegate_result",
            causation_id=child.execution_id,
        )
        if parent.status is ExecutionStatus.WAITING_CHILDREN:
            self.change_state(connection, parent, self.resumable_state(connection, parent))

    def pending_deliveries(
        self, *, limit: int = 100, excluded_targets: frozenset[tuple[str, str]] | None = None
    ) -> tuple[Delivery, ...]:
        """Read recoverable delivery intents without consuming or rewriting SDK inputs."""
        self.validate_page(limit, 0)
        with self.transaction() as connection:
            now = datetime.now(UTC)
            return tuple(
                value
                for value in self.records(connection, "deliveries", Delivery)
                if value.state == "pending"
                and (value.next_due is None or value.next_due <= now)
                and (excluded_targets is None or (value.target_kind, value.target_id) not in excluded_targets)
            )[:limit]

    def confirm_delivery(self, delivery_id: str, *, expected_generation: int, sdk_sequence: int) -> Delivery:
        """Confirm the SDK's durable receipt independently of model consumption."""
        with self.transaction(write=True) as connection:
            current = self.require(connection, "deliveries", delivery_id, Delivery)
            self.check_revision(current.generation, expected_generation)
            if current.state == "delivered":
                if current.sdk_sequence != sdk_sequence:
                    raise ProductConflict("The SDK receipt sequence changed")
                return current
            value = current.model_copy(
                update={
                    "state": "delivered",
                    "sdk_sequence": sdk_sequence,
                    "attempts": current.attempts + 1,
                    "last_error": None,
                }
            )
            self.save(
                connection,
                "deliveries",
                delivery_id,
                value,
                input_id=value.input_id,
                state=value.state,
                generation=value.generation,
            )
            self.emit(connection, delivery_id, "delivery.accepted", value.model_dump(mode="json"))
            return value

    def fail_delivery(self, delivery_id: str, *, expected_generation: int, error: str) -> Delivery:
        """Persist an actionable delivery failure while retaining the original payload."""
        with self.transaction(write=True) as connection:
            current = self.require(connection, "deliveries", delivery_id, Delivery)
            self.check_revision(current.generation, expected_generation)
            if current.state == "delivered":
                return current
            value = current.model_copy(
                update={"state": "failed", "last_error": error, "attempts": current.attempts + 1}
            )
            self.save(
                connection,
                "deliveries",
                delivery_id,
                value,
                input_id=value.input_id,
                state=value.state,
                generation=value.generation,
            )
            self.emit(connection, delivery_id, "delivery.failed", value.model_dump(mode="json"))
            return value

    def delivery_view(self, connection: sqlite3.Connection, delivery: Delivery) -> DeliveryView:
        """Derive manual retry from the current delivery and its actual owning scope."""
        reason = "Only failed deliveries require a manual retry"
        actionable = delivery.state == "failed"
        if actionable and delivery.target_kind == "execution":
            execution = self.require(connection, "executions", delivery.target_id, Execution)
            space = self.require(connection, "spaces", execution.space_id, Space)
            item = self.require(connection, "inbox_items", delivery.input_id, InboxItem)
            disposition = self.load(
                connection,
                "input_dispositions",
                stable_id(f"disposition/{item.origin_input_id}/{execution.execution_id}"),
                InputDisposition,
            )
            actionable = (
                execution.status not in TERMINAL_STATUSES
                and execution.status is not ExecutionStatus.CANCELLING
                and execution.control_intent != "cancel"
                and not space.deleted
                and not space.deletion_pending
                and (disposition is None or disposition.state == "owned")
            )
            reason = "The target no longer accepts this input responsibility"
        return DeliveryView(
            **delivery.model_dump(),
            available_actions=("retry",) if actionable else (),
            disabled_reasons={} if actionable else {"retry": reason},
        )

    def retry_delivery(self, delivery_id: str, *, request_id: str, expected_generation: int) -> Delivery:
        """Retry product delivery alone without creating another Worker execution."""
        digest = fingerprint({"delivery_id": delivery_id, "expected_generation": expected_generation})
        with self.transaction(write=True) as connection:
            old = self.receipt(connection, f"delivery/retry/{request_id}", digest, Delivery)
            if old is not None:
                return old
            current = self.require(connection, "deliveries", delivery_id, Delivery)
            self.check_revision(current.generation, expected_generation)
            if (
                current.state != "delivered"
                and "retry" not in self.delivery_view(connection, current).available_actions
            ):
                raise ProductConflict("The delivery no longer permits a manual retry")
            value = (
                current
                if current.state == "delivered"
                else current.model_copy(
                    update={
                        "state": "pending",
                        "generation": current.generation + 1,
                        "last_error": None,
                        "next_due": None,
                    }
                )
            )
            self.save(
                connection,
                "deliveries",
                delivery_id,
                value,
                input_id=value.input_id,
                state=value.state,
                generation=value.generation,
            )
            self.save_receipt(connection, f"delivery/retry/{request_id}", digest, value)
            if value != current:
                self.emit(connection, delivery_id, "delivery.retried", value.model_dump(mode="json"))
            return value

    def claim_router_batch(self, *, limit: int = 20) -> RouteAttempt | None:
        """Fix one immutable Router batch, requiring a genuine wake source."""
        self.validate_page(limit, 0)
        with self.transaction(write=True) as connection:
            active = connection.execute(
                "SELECT decision_id,attempt FROM route_batches WHERE state IN ('pending','candidate') "
                "ORDER BY rowid LIMIT 1"
            ).fetchone()
            if active is not None:
                return self.require(
                    connection, "route_attempts", f"{active['decision_id']}/{active['attempt']}", RouteAttempt
                )
            inputs = tuple(
                value
                for value in self.records(connection, "inbox_items", InboxItem)
                if value.target_kind == "router" and value.status == "pending"
            )
            if not any(value.wake_policy == "wake" for value in inputs):
                return None
            selected_list: list[InboxItem] = []
            selected_bytes = 0
            for item in inputs[:limit]:
                item_bytes = len(item.content.encode("utf-8"))
                if selected_list and selected_bytes + item_bytes > ROUTER_BATCH_SOURCE_BYTES:
                    break
                selected_list.append(item)
                selected_bytes += item_bytes
            selected = tuple(selected_list)
            if not any(value.wake_policy == "wake" for value in selected):
                waking = next(value for value in inputs if value.wake_policy == "wake")
                while selected and (
                    len(selected) >= limit
                    or selected_bytes + len(waking.content.encode("utf-8")) > ROUTER_BATCH_SOURCE_BYTES
                ):
                    selected_bytes -= len(selected[-1].content.encode("utf-8"))
                    selected = selected[:-1]
                selected = (*selected, waking)
            identities = tuple(value.input_id for value in selected)
            digest = fingerprint({"source_input_ids": list(identities)})
            decision_id = stable_id(f"route/{digest}")
            value = RouteAttempt(
                decision_id=decision_id,
                invocation_id=f"{decision_id}/1",
                source_input_ids=identities,
                sdk_input_ids=identities,
            )
            connection.execute("INSERT INTO route_batches VALUES(?,?,1,'pending',NULL)", (decision_id, digest))
            self.save(
                connection,
                "route_attempts",
                f"{decision_id}/1",
                value,
                decision_id=decision_id,
                invocation_id=value.invocation_id,
            )
            for item in selected:
                self.save_input(connection, item.model_copy(update={"status": "claimed"}))
            return value

    def save_route_candidate(self, decision: RouteDecision, *, attempt: int, sdk_run_id: str) -> RouteAttempt:
        """Persist the SDK result before starting the product application transaction."""
        with self.transaction(write=True) as connection:
            batch = connection.execute(
                "SELECT attempt,state FROM route_batches WHERE decision_id=?", (decision.decision_id,)
            ).fetchone()
            if batch is None:
                raise LookupError("The Router batch does not exist")
            if batch["attempt"] != attempt:
                raise ProductConflict("The routing candidate belongs to an obsolete attempt")
            current = self.require(connection, "route_attempts", f"{decision.decision_id}/{attempt}", RouteAttempt)
            if current.source_input_ids != decision.source_input_ids:
                raise ProductConflict("A Router candidate cannot alter the fixed product batch")
            if current.candidate is not None:
                if current.candidate != decision or current.sdk_run_id != sdk_run_id:
                    raise ProductConflict("The routing attempt already has a different candidate")
                return current
            value = current.model_copy(update={"candidate": decision, "sdk_run_id": sdk_run_id, "state": "candidate"})
            self.save(
                connection,
                "route_attempts",
                f"{decision.decision_id}/{attempt}",
                value,
                decision_id=decision.decision_id,
                invocation_id=value.invocation_id,
            )
            connection.execute(
                "UPDATE route_batches SET state='candidate' WHERE decision_id=?", (decision.decision_id,)
            )
            return value

    def bind_route_run(self, decision_id: str, attempt: int, run_id: str) -> RouteAttempt:
        """Associate an accepted SDK invocation without changing the immutable product batch."""
        with self.transaction(write=True) as connection:
            batch = connection.execute(
                "SELECT attempt FROM route_batches WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if batch is None:
                raise LookupError("The Router batch does not exist")
            if batch["attempt"] != attempt:
                raise ProductConflict("The SDK Run belongs to an obsolete Router attempt")
            current = self.require(connection, "route_attempts", f"{decision_id}/{attempt}", RouteAttempt)
            if current.sdk_run_id is not None:
                if current.sdk_run_id != run_id:
                    raise ProductConflict("The Router attempt already belongs to another SDK Run")
                return current
            value = current.model_copy(update={"sdk_run_id": run_id})
            self.save(
                connection,
                "route_attempts",
                f"{decision_id}/{attempt}",
                value,
                decision_id=decision_id,
                invocation_id=current.invocation_id,
            )
            return value

    def check_read_versions(self, connection: sqlite3.Connection, versions: dict[str, int]) -> None:
        """Validate only objects read by this candidate, independent of unrelated inputs."""
        for key, revision in versions.items():
            kind, separator, identity = key.partition(":")
            if not separator:
                raise ValueError("Read-version keys use kind:identity")
            if kind == "space":
                current = self.require(connection, "spaces", identity, Space).revision
            elif kind == "execution":
                current = self.require(connection, "executions", identity, Execution).revision
            elif kind == "attention":
                current = self.require(connection, "attentions", identity, Attention).revision
            elif kind == "input":
                current = self.require(connection, "inbox_items", identity, InboxItem).routing_revision
            elif kind == "disposition":
                current = self.require(connection, "input_dispositions", identity, InputDisposition).revision
            else:
                raise ValueError("Unsupported Router read-version object")
            self.check_revision(current, revision)

    def require_route_version(self, candidate: RouteDecision, kind: str, identity: str) -> None:
        """Reject effects whose optimistic concurrency evidence was never supplied."""
        if f"{kind}:{identity}" not in candidate.read_versions:
            raise ProductConflict(f"The candidate did not capture the {kind} revision")

    def routing_inputs(self, connection: sqlite3.Connection, input_ids: tuple[str, ...]) -> tuple[InboxItem, ...]:
        """Read an answer and the immutable requests owned by its resolved global question."""
        questions = {
            value.resolution_input_id: value
            for value in self.records(connection, "attentions", Attention)
            if value.execution_id is None and value.status == "resolved" and value.resolution_input_id is not None
        }
        pending = list(input_ids)
        result: dict[str, InboxItem] = {}
        while pending:
            identity = pending.pop(0)
            if identity in result:
                continue
            source = self.require(connection, "inbox_items", identity, InboxItem)
            result[identity] = source
            if source.source == "weave.attention" and source.origin_input_id in questions:
                pending.extend(questions[source.origin_input_id].source_input_ids)
        return tuple(sorted(result.values(), key=lambda value: (value.created_at, value.input_id)))

    def route_origins(
        self, connection: sqlite3.Connection, candidate: RouteDecision, input_ids: tuple[str, ...]
    ) -> tuple[InboxItem, ...]:
        """Resolve only the sources owned by this product batch and responsibility barrier."""
        ids = input_ids if input_ids else candidate.source_input_ids
        if not set(ids) <= set(candidate.source_input_ids):
            raise ProductConflict("A routing action cannot use an input outside its batch")
        result: dict[str, InboxItem] = {}
        for source in self.routing_inputs(connection, ids):
            if source.source in {"weave.report", "weave.control"}:
                continue
            origin = self.require(connection, "inbox_items", source.origin_input_id, InboxItem)
            handbacks = tuple(
                value
                for value in self.records(connection, "input_handbacks", Handback)
                if value.origin_input_id == origin.input_id
            )
            if origin.wake_policy == "silent" and not handbacks:
                raise ProductConflict("Explicitly targeted input already belongs to its assigned Workers")
            if handbacks:
                self.require_route_version(candidate, "input", origin.input_id)
                if any(
                    value.origin_input_id == origin.input_id and value.state == "owned"
                    for value in self.records(connection, "input_dispositions", InputDisposition)
                ):
                    raise ProductConflict("The original input still has an owning Worker")
                explicitly_clarified = source.input_id not in ids
                if (origin.reroute_round >= 2 and not explicitly_clarified) or bool(
                    self.unresolved_handbacks(connection, tuple(handbacks))
                ):
                    raise ProductConflict("The returned input requires explicit clarification or recovery")
            result[origin.input_id] = origin
        return tuple(result.values())

    def apply_route_decision(self, decision_id: str, *, attempt: int) -> RouteReceipt:
        """Apply all routing effects, formal reply, and input receipts atomically."""
        with self.transaction(write=True) as connection:
            batch = connection.execute(
                "SELECT attempt,state,receipt FROM route_batches WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if batch is None:
                raise LookupError("The Router batch does not exist")
            if batch["state"] == "applied":
                return RouteReceipt.model_validate_json(batch["receipt"])
            if batch["attempt"] != attempt:
                raise ProductConflict("The routing attempt is obsolete")
            current = self.require(connection, "route_attempts", f"{decision_id}/{attempt}", RouteAttempt)
            candidate = current.candidate
            if candidate is None:
                raise ProductConflict("The routing attempt has no persisted candidate")
            self.check_read_versions(connection, candidate.read_versions)
            clarified: set[str] = set()
            for action in candidate.actions:
                if isinstance(action, RequestClarificationAction):
                    origins = self.route_origins(connection, candidate, action.source_input_ids)
                    identities = {value.input_id for value in origins}
                    if not identities or clarified.intersection(identities):
                        raise ProductConflict("A request must belong to one concrete clarification")
                    for question in self.records(connection, "attentions", Attention):
                        if question.execution_id is None and question.status == "pending":
                            pending_ids = {
                                value.origin_input_id
                                for value in self.routing_inputs(connection, question.source_input_ids)
                            }
                            if identities.intersection(pending_ids):
                                raise ProductConflict("The request already has a pending clarification")
                    clarified.update(identities)
            created: dict[str, str] = {}
            for action in candidate.actions:
                if not isinstance(action, CreateSpaceAction):
                    continue
                if action.local_ref in created:
                    raise ProductConflict("A routing decision repeats a Space local reference")
                identity = stable_id(f"route-space/{decision_id}/{action.local_ref}")
                space = Space(**action.configuration.model_dump(), space_id=identity)
                self.save_space(connection, space)
                self.emit(connection, identity, "space.created", space.model_dump(mode="json"))
                created[action.local_ref] = identity
            execution_ids: list[str] = []
            attention_ids: list[str] = []
            rerouted: dict[str, InboxItem] = {}
            for index, action in enumerate(candidate.actions):
                if isinstance(action, CreateSpaceAction):
                    continue
                elif isinstance(action, DispatchAction):
                    if action.space_ref.kind == "existing":
                        space_id = action.space_ref.id
                        self.require_route_version(candidate, "space", space_id)
                    else:
                        if action.space_ref.id not in created:
                            raise ProductConflict("The created Space reference is not defined earlier in this decision")
                        space_id = created[action.space_ref.id]
                    execution = self.insert_execution(
                        connection,
                        ExecutionSpec(
                            space_id=space_id,
                            objective=action.objective,
                            delivery_contract=action.delivery_contract,
                            budget_limit=action.budget_limit,
                        ),
                        stable_id(f"route-execution/{decision_id}/{index}"),
                    )
                    execution_ids.append(execution.execution_id)
                    for origin in self.route_origins(connection, candidate, action.source_input_ids):
                        if origin.input_id in clarified:
                            raise ProductConflict("A request cannot be dispatched while asking for clarification")
                        self.assign_input(connection, origin, execution.execution_id)
                        if (
                            connection.execute(
                                "SELECT 1 FROM input_handbacks WHERE origin_id=?", (origin.input_id,)
                            ).fetchone()
                            is not None
                        ):
                            rerouted[origin.input_id] = origin
                elif isinstance(action, DeliverInputAction):
                    self.require_route_version(candidate, "execution", action.execution_id)
                    for origin in self.route_origins(connection, candidate, action.source_input_ids):
                        if origin.input_id in clarified:
                            raise ProductConflict("A request cannot be delivered while asking for clarification")
                        self.assign_input(connection, origin, action.execution_id)
                        if (
                            connection.execute(
                                "SELECT 1 FROM input_handbacks WHERE origin_id=?", (origin.input_id,)
                            ).fetchone()
                            is not None
                        ):
                            rerouted[origin.input_id] = origin
                elif isinstance(action, RequestClarificationAction):
                    identity = stable_id(f"route-attention/{decision_id}/{index}")
                    question = Attention(
                        attention_id=identity,
                        execution_id=None,
                        kind="clarification",
                        question=action.question,
                        options=action.options,
                        source_input_ids=action.source_input_ids,
                    )
                    self.save_attention(connection, question)
                    self.emit(connection, identity, "attention.created", question.model_dump(mode="json"))
                    attention_ids.append(identity)
                elif isinstance(action, RequestCancelAction):
                    self.require_route_version(candidate, "execution", action.execution_id)
                    execution = self.writable_execution(connection, action.execution_id)
                    self.change_state(connection, execution, ExecutionStatus.CANCELLING, control_intent="cancel")
                    self.cancel_children(connection, action.execution_id)
                elif isinstance(action, AnswerQuestionAction):
                    self.require_route_version(candidate, "attention", action.attention_id)
                    attention = self.require(connection, "attentions", action.attention_id, Attention)
                    if attention.kind == "approval":
                        raise ProductConflict("Router text cannot grant action approval")
                    if attention.status != "pending":
                        raise ProductConflict("The question has already been resolved")
                    if action.source_input_id not in candidate.source_input_ids:
                        raise ProductConflict("A business answer must identify its source in the fixed batch")
                    source = self.require(connection, "inbox_items", action.source_input_id, InboxItem)
                    if source.kind != "user" or source.causation_id is None:
                        raise ProductConflict("A business answer requires a real user input source")
                    row = connection.execute(
                        "SELECT payload FROM chat_messages WHERE id=?", (source.causation_id,)
                    ).fetchone()
                    if row is None:
                        raise ProductConflict("The business answer source has no saved user message")
                    answer_message = Message.model_validate_json(row["payload"])
                    if answer_message.role != "user":
                        raise ProductConflict("A business answer requires a real user message")
                    self.resolve_in_transaction(
                        connection,
                        attention,
                        ResolutionRequest(
                            request_id=f"route/{decision_id}/{index}",
                            expected_revision=attention.revision,
                            answer=action.answer,
                            attachment_ids=answer_message.attachment_ids,
                            context_refs=answer_message.context_refs,
                        ),
                        source_message=answer_message,
                    )
            for origin in rerouted.values():
                self.save_input(
                    connection,
                    origin.model_copy(
                        update={
                            "routing_revision": origin.routing_revision + 1,
                            "reroute_round": origin.reroute_round + 1,
                        }
                    ),
                )
            message: Message | None = None
            if candidate.reply.strip():
                source_message_ids = tuple(
                    source.causation_id
                    for identity in candidate.source_input_ids
                    if (source := self.require(connection, "inbox_items", identity, InboxItem)).source
                    == "weave.message"
                    and source.causation_id is not None
                )
                report_ids = tuple(
                    source.causation_id
                    for identity in candidate.source_input_ids
                    if (source := self.require(connection, "inbox_items", identity, InboxItem)).source == "weave.report"
                    and source.causation_id is not None
                )
                message = self.insert_message(
                    connection,
                    source_key=f"route/{decision_id}",
                    role="assistant",
                    body=candidate.reply,
                    target_execution_ids=tuple(execution_ids),
                    report_ids=report_ids,
                    source_message_ids=source_message_ids,
                )
            for identity in candidate.source_input_ids:
                source = self.require(connection, "inbox_items", identity, InboxItem)
                self.save_input(connection, source.model_copy(update={"status": "handled"}))
            value = RouteReceipt(
                decision_id=decision_id,
                attempt=attempt,
                execution_ids=tuple(execution_ids),
                space_ids=tuple(created.values()),
                attention_ids=tuple(attention_ids),
                message_id=None if message is None else message.message_id,
            )
            self.save(
                connection,
                "route_attempts",
                f"{decision_id}/{attempt}",
                current.model_copy(update={"state": "applied"}),
                decision_id=decision_id,
                invocation_id=current.invocation_id,
            )
            connection.execute(
                "UPDATE route_batches SET state='applied',receipt=? WHERE decision_id=?",
                (value.model_dump_json(), decision_id),
            )
            self.emit(connection, decision_id, "route.applied", value.model_dump(mode="json"))
            return value

    def reassess_route(self, decision_id: str, *, reason: str) -> RouteAttempt:
        """Create a new observation without reclaiming the original SDK inputs."""
        with self.transaction(write=True) as connection:
            batch = connection.execute(
                "SELECT attempt,state FROM route_batches WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if batch is None:
                raise LookupError("The routing batch does not exist")
            if batch["state"] == "applied":
                raise ProductConflict("Applied routing decisions cannot be reassessed")
            current = self.require(connection, "route_attempts", f"{decision_id}/{batch['attempt']}", RouteAttempt)
            failed = current.model_copy(update={"state": "failed", "failure_reason": reason})
            self.save(
                connection,
                "route_attempts",
                f"{decision_id}/{current.attempt}",
                failed,
                decision_id=decision_id,
                invocation_id=current.invocation_id,
            )
            if current.attempt >= 3:
                connection.execute("UPDATE route_batches SET state='failed' WHERE decision_id=?", (decision_id,))
                identity = stable_id(f"routing-failed/{decision_id}")
                if self.load(connection, "attentions", identity, Attention) is None:
                    sources = tuple(
                        self.require(connection, "inbox_items", value, InboxItem).content
                        for value in current.source_input_ids
                    )
                    attention = Attention(
                        attention_id=identity,
                        execution_id=None,
                        kind="clarification",
                        question="Routing could not be applied. Clarify this request to continue. "
                        + "\n".join(sources)[:2048],
                        source_input_ids=current.source_input_ids,
                    )
                    self.save_attention(connection, attention)
                    self.emit(connection, identity, "attention.created", attention.model_dump(mode="json"))
                self.emit(
                    connection,
                    decision_id,
                    "route.failed",
                    {"reason": reason, "source_input_ids": list(current.source_input_ids)},
                )
                return failed
            attempt = current.attempt + 1
            input_id = f"{decision_id}/{attempt}/reassessment"
            content = json.dumps(
                {
                    "source_input_ids": list(current.source_input_ids),
                    "unapplied_candidate": None
                    if current.candidate is None
                    else current.candidate.model_dump(mode="json"),
                    "reason": reason,
                },
                ensure_ascii=False,
            )
            source = self.enqueue(
                connection,
                input_id=input_id,
                origin_input_id=input_id,
                target_kind="router",
                target_id="router",
                content=content,
                source="weave.reassessment",
                wake_policy="wake",
                causation_id=decision_id,
            )
            self.save_input(connection, source.model_copy(update={"status": "claimed"}))
            value = RouteAttempt(
                decision_id=decision_id,
                attempt=attempt,
                invocation_id=f"{decision_id}/{attempt}",
                source_input_ids=current.source_input_ids,
                sdk_input_ids=(input_id,),
            )
            self.save(
                connection,
                "route_attempts",
                f"{decision_id}/{attempt}",
                value,
                decision_id=decision_id,
                invocation_id=value.invocation_id,
            )
            connection.execute(
                "UPDATE route_batches SET attempt=?,state='pending' WHERE decision_id=?", (attempt, decision_id)
            )
            return value

    def get_disposition(self, origin_input_id: str, execution_id: str) -> InputDisposition | None:
        """Read the responsibility of one exact target for an original input."""
        with self.transaction() as connection:
            return self.load(
                connection,
                "input_dispositions",
                stable_id(f"disposition/{origin_input_id}/{execution_id}"),
                InputDisposition,
            )

    def handle_input(
        self, input_id: str, *, execution_id: str, expected_revision: int, outcome_refs: tuple[str, ...]
    ) -> InputDisposition:
        """Close responsibility only against persisted output evidence."""
        if not outcome_refs:
            raise ValueError("Handled inputs require persisted outcome evidence")
        with self.transaction(write=True) as connection:
            item = self.require(connection, "inbox_items", input_id, InboxItem)
            if item.target_kind != "execution" or item.target_id != execution_id:
                raise ProductConflict("The input belongs to another Worker")
            identity = stable_id(f"disposition/{item.origin_input_id}/{execution_id}")
            current = self.require(connection, "input_dispositions", identity, InputDisposition)
            if current.state == "handled" and current.outcome_refs == outcome_refs:
                return current
            self.check_revision(current.revision, expected_revision)
            if current.state != "owned":
                raise ProductConflict("The input responsibility is already disposed")
            for outcome in outcome_refs:
                report = self.load(connection, "reports", outcome, Report)
                artifact = self.load(connection, "artifacts", outcome, Artifact)
                if (report is None or report.execution_id != execution_id) and (
                    artifact is None or artifact.owner_execution_id != execution_id
                ):
                    raise ProductConflict("The handling evidence does not belong to this Execution")
            value = current.model_copy(
                update={"state": "handled", "outcome_refs": outcome_refs, "revision": current.revision + 1}
            )
            self.save_disposition(connection, value)
            self.save_input(connection, item.model_copy(update={"status": "handled"}))
            self.activate_handback(connection, item.origin_input_id)
            self.emit(connection, execution_id, "input.handled", value.model_dump(mode="json"))
            return value

    def return_to_router(
        self,
        input_id: str,
        *,
        execution_id: str,
        expected_revision: int,
        reason: str,
        already_handled_refs: tuple[str, ...] = (),
        effect_refs: tuple[str, ...] = (),
        unknown_effects: bool = False,
        action_refs: tuple[ActionRef, ...] = (),
    ) -> Handback:
        """Atomically return a single target responsibility and preserve prior effects."""
        with self.transaction(write=True) as connection:
            return self.return_in_transaction(
                connection,
                input_id,
                execution_id=execution_id,
                expected_revision=expected_revision,
                reason=reason,
                already_handled_refs=already_handled_refs,
                effect_refs=effect_refs,
                unknown_effects=unknown_effects,
                action_refs=action_refs,
            )

    def return_in_transaction(
        self,
        connection: sqlite3.Connection,
        input_id: str,
        *,
        execution_id: str,
        expected_revision: int,
        reason: str,
        already_handled_refs: tuple[str, ...] = (),
        effect_refs: tuple[str, ...] = (),
        unknown_effects: bool = False,
        action_refs: tuple[ActionRef, ...] = (),
    ) -> Handback:
        """Record handback under the caller's existing terminal or input transaction."""
        item = self.require(connection, "inbox_items", input_id, InboxItem)
        if item.target_kind != "execution" or item.target_id != execution_id:
            raise ProductConflict("A Worker can only return its own input")
        origin = self.require(connection, "inbox_items", item.origin_input_id, InboxItem)
        identity = stable_id(f"handback/{item.origin_input_id}/{execution_id}")
        execution = self.require(connection, "executions", execution_id, Execution)
        refs = action_refs
        if unknown_effects and not refs and execution.current_run_id is not None:
            refs = tuple(
                ActionRef(run_id=execution.current_run_id, call_id=identity) for identity in execution.unknown_effects
            )
        value = Handback(
            handback_id=identity,
            origin_input_id=item.origin_input_id,
            target_execution_id=execution_id,
            input_id=input_id,
            reason=reason,
            already_handled_refs=already_handled_refs,
            effect_refs=effect_refs,
            unknown_effects=unknown_effects,
            run_id=execution.current_run_id,
            action_refs=refs,
            reroute_round=origin.reroute_round,
        )
        old = self.load(connection, "input_handbacks", identity, Handback)
        if old is not None:
            if old.model_dump(exclude={"reroute_round"}) != value.model_dump(exclude={"reroute_round"}):
                raise ProductConflict("The handback identity has conflicting effects or reason")
            return old
        self.writable_execution(connection, execution_id)
        disposition = self.require(
            connection,
            "input_dispositions",
            stable_id(f"disposition/{item.origin_input_id}/{execution_id}"),
            InputDisposition,
        )
        self.check_revision(disposition.revision, expected_revision)
        if disposition.state != "owned":
            raise ProductConflict("The input was already disposed by this Worker")
        self.save(
            connection, "input_handbacks", identity, value, origin_id=item.origin_input_id, target_id=execution_id
        )
        self.save_disposition(
            connection,
            disposition.model_copy(
                update={"state": "returned", "handback_id": identity, "revision": disposition.revision + 1}
            ),
        )
        self.save_input(connection, item.model_copy(update={"status": "handled"}))
        self.activate_handback(connection, item.origin_input_id)
        self.emit(connection, execution_id, "input.returned", value.model_dump(mode="json"))
        return value

    def unresolved_handbacks(
        self, connection: sqlite3.Connection, handbacks: tuple[Handback, ...]
    ) -> tuple[Handback, ...]:
        """Read unresolved historical declarations using their separate trusted settlements."""
        result = []
        for value in handbacks:
            if not value.unknown_effects:
                continue
            resolved = self.load(connection, "input_handback_resolutions", value.handback_id, HandbackResolution)
            if resolved is None or resolved.run_id != value.run_id or resolved.action_refs != value.action_refs:
                result.append(value)
        return tuple(result)

    def settle_handbacks(
        self, execution_id: str, *, reconciliation: ReconcileRecord, actions: tuple[ToolCallRecord, ...]
    ) -> tuple[HandbackResolution, ...]:
        """Release original requests only after matching SDK facts have settled every declared action."""
        with self.transaction(write=True) as connection:
            execution = self.require(connection, "executions", execution_id, Execution)
            if execution.current_run_id != reconciliation.run_id:
                raise ProductConflict("Handback settlement requires its original SDK invocation")
            recovered = ReconcileResult.model_validate(reconciliation.result)
            if recovered.unsettled_actions or recovered.snapshot != reconciliation.snapshot:
                raise ProductConflict("Handback settlement requires a completed trusted SDK reconciliation")
            facts = {(value.run_id, value.call_id): value for value in actions}
            candidates = tuple(
                value
                for value in self.records(connection, "input_handbacks", Handback)
                if value.target_execution_id == execution_id and value.run_id == reconciliation.run_id
            )
            resolved = []
            origins: set[str] = set()
            for value in self.unresolved_handbacks(connection, candidates):
                calls = [facts.get((ref.run_id, ref.call_id)) for ref in value.action_refs]
                if any(call is None or call.state not in {"succeeded", "failed", "cancelled"} for call in calls):
                    continue
                if any(
                    ref.operation_id is not None and call is not None and call.operation_id != ref.operation_id
                    for ref, call in zip(value.action_refs, calls, strict=True)
                ):
                    raise ProductConflict("The trusted fact belongs to another original operation")
                resolution = HandbackResolution(
                    handback_id=value.handback_id,
                    run_id=reconciliation.run_id,
                    action_refs=value.action_refs,
                    reconciliation_id=reconciliation.request_id,
                    evidence_ids=reconciliation.evidence_ids,
                    reconciled_revision=reconciliation.snapshot.revision,
                    resolved_at=reconciliation.accepted_at,
                )
                self.save(connection, "input_handback_resolutions", value.handback_id, resolution)
                self.emit(connection, value.handback_id, "input.effects_resolved", resolution.model_dump(mode="json"))
                resolved.append(resolution)
                origins.add(value.origin_input_id)
            for origin_id in origins:
                remaining = tuple(
                    value
                    for value in self.records(connection, "input_handbacks", Handback)
                    if value.origin_input_id == origin_id
                )
                if self.unresolved_handbacks(connection, remaining):
                    continue
                for attention in self.records(connection, "attentions", Attention):
                    if (
                        attention.kind == "recovery"
                        and attention.status == "pending"
                        and origin_id in attention.source_input_ids
                    ):
                        withdrawn = attention.model_copy(
                            update={"status": "withdrawn", "revision": attention.revision + 1}
                        )
                        self.save_attention(connection, withdrawn)
                        self.emit(
                            connection, attention.attention_id, "attention.withdrawn", withdrawn.model_dump(mode="json")
                        )
                self.activate_handback(connection, origin_id)
            return tuple(resolved)

    def activate_handback(self, connection: sqlite3.Connection, origin_input_id: str) -> None:
        """Wake the Router once only after every target responsibility is disposed."""
        dispositions = tuple(
            value
            for value in self.records(connection, "input_dispositions", InputDisposition)
            if value.origin_input_id == origin_input_id
        )
        if any(value.state == "owned" for value in dispositions):
            return
        origin = self.require(connection, "inbox_items", origin_input_id, InboxItem)
        handbacks = tuple(
            value
            for value in self.records(connection, "input_handbacks", Handback)
            if value.origin_input_id == origin_input_id and value.reroute_round == origin.reroute_round
        )
        if not handbacks:
            return
        unknown = bool(self.unresolved_handbacks(connection, handbacks))
        if origin.reroute_round >= 2 or unknown:
            kind = "recovery" if unknown else "clarification"
            identity = stable_id(f"handback-attention/{origin_input_id}/{origin.reroute_round}/{kind}")
            attention = Attention(
                attention_id=identity,
                execution_id=None,
                kind="recovery" if unknown else "clarification",
                question="Please clarify the remaining request or reconcile its unknown effects",
                source_input_ids=(origin_input_id,),
            )
            if self.load(connection, "attentions", identity, Attention) is None:
                self.save_attention(connection, attention)
                self.emit(connection, identity, "attention.created", attention.model_dump(mode="json"))
            return
        input_id = f"{origin_input_id}/{origin.reroute_round}/handback_ready"
        self.enqueue(
            connection,
            input_id=input_id,
            origin_input_id=origin_input_id,
            target_kind="router",
            target_id="router",
            content=json.dumps(
                {"origin_input_id": origin_input_id, "handback_ids": [value.handback_id for value in handbacks]},
                ensure_ascii=False,
            ),
            source="weave.handback",
            wake_policy="wake",
            causation_id=origin_input_id,
        )

    def save_attention(self, connection: sqlite3.Connection, value: Attention) -> None:
        """Save an independently revisioned user-decision record."""
        self.save(
            connection, "attentions", value.attention_id, value, execution_id=value.execution_id, status=value.status
        )

    def create_attention(self, value: Attention) -> Attention:
        """Persist one host-validated concrete question or action approval."""
        with self.transaction(write=True) as connection:
            old = self.load(connection, "attentions", value.attention_id, Attention)
            if old is not None:
                if old != value:
                    raise ProductConflict("The Attention identity already has different content")
                return old
            if value.execution_id is not None:
                self.writable_execution(connection, value.execution_id)
            self.save_attention(connection, value)
            self.emit(connection, value.attention_id, "attention.created", value.model_dump(mode="json"))
            if value.execution_id is not None:
                execution = self.require(connection, "executions", value.execution_id, Execution)
                if value.kind != "recovery":
                    target = self.resumable_state(connection, execution)
                    self.change_state(
                        connection,
                        execution,
                        target,
                        reason=execution.reason if target is ExecutionStatus.BLOCKED else None,
                    )
            return value

    def resolve_in_transaction(
        self,
        connection: sqlite3.Connection,
        current: Attention,
        request: ResolutionRequest,
        *,
        source_message: Message | None = None,
    ) -> ResolutionReceipt:
        """Commit one answer, all references, and the exact recovery delivery together."""
        self.check_revision(current.revision, request.expected_revision)
        if current.status != "pending":
            raise ProductConflict("The Attention is no longer pending", current_revision=current.revision)
        if current.expires_at is not None and current.expires_at <= datetime.now(UTC):
            raise ProductConflict("The Attention decision expired", current_revision=current.revision)
        if current.kind == "approval":
            if request.decision is None or request.action_digest != current.action_digest:
                raise ProductConflict("Explicit approval requires the current action digest")
        elif request.answer is None:
            raise ProductConflict("Business questions require an answer")
        if current.options and request.answer is not None and request.answer not in current.options:
            raise ValueError("The answer must match one of the valid options")
        self.validate_references(connection, request.attachment_ids, request.context_refs)
        execution = None if current.execution_id is None else self.writable_execution(connection, current.execution_id)
        body = request.answer if request.answer is not None else request.decision
        if body is None:
            raise ValueError("The resolution has no answer or decision")
        message = source_message
        if message is None:
            message = self.insert_message(
                connection,
                source_key=f"attention/{request.request_id}",
                role="user",
                body=body,
                attachment_ids=request.attachment_ids,
                context_refs=request.context_refs,
                target_execution_ids=() if execution is None else (execution.execution_id,),
            )
        origin_id = stable_id(f"attention-input/{request.request_id}")
        updated = current.model_copy(
            update={
                "status": "resolved",
                "resolution": body,
                "resolution_source": message.message_id,
                "resolution_input_id": origin_id,
                "revision": current.revision + 1,
            }
        )
        self.save_attention(connection, updated)
        origin = self.enqueue(
            connection,
            input_id=origin_id,
            origin_input_id=origin_id,
            target_kind="router",
            target_id="router",
            content=json.dumps(
                {
                    "attention_id": current.attention_id,
                    "question": current.question,
                    "answer": body,
                    "source_input_ids": list(current.source_input_ids),
                },
                ensure_ascii=False,
            ),
            source="weave.attention",
            wake_policy="wake" if execution is None else "silent",
            kind="user",
            causation_id=message.message_id,
        )
        if execution is not None:
            self.assign_input(connection, origin, execution.execution_id)
            target = self.resumable_state(connection, execution)
            if execution.status is not ExecutionStatus.RUNNING:
                execution = self.change_state(
                    connection,
                    execution,
                    target,
                    reason="recovery_unknown" if target is ExecutionStatus.BLOCKED else None,
                )
        self.emit(connection, current.attention_id, "attention.resolved", updated.model_dump(mode="json"))
        return ResolutionReceipt(
            message_id=message.message_id, attention_revision=updated.revision, execution=execution
        )

    def resolve_attention(self, attention_id: str, request: ResolutionRequest) -> ResolutionReceipt:
        """Apply exactly one explicit answer with full-payload idempotency."""
        digest = fingerprint({"attention_id": attention_id, "request": request.model_dump(mode="json")})
        with self.transaction(write=True) as connection:
            old = self.receipt(connection, f"attention/resolve/{request.request_id}", digest, ResolutionReceipt)
            if old is not None:
                return old
            current = self.require(connection, "attentions", attention_id, Attention)
            value = self.resolve_in_transaction(connection, current, request)
            self.save_receipt(connection, f"attention/resolve/{request.request_id}", digest, value)
            return value

    def expire_attentions(self, *, now: datetime | None = None) -> tuple[Attention, ...]:
        """Expire only still-pending questions and leave historical decisions immutable."""
        instant = datetime.now(UTC) if now is None else now
        changed: list[Attention] = []
        with self.transaction(write=True) as connection:
            for current in self.records(connection, "attentions", Attention):
                if current.status == "pending" and current.expires_at is not None and current.expires_at <= instant:
                    value = current.model_copy(update={"status": "expired", "revision": current.revision + 1})
                    self.save_attention(connection, value)
                    self.emit(connection, current.attention_id, "attention.expired", value.model_dump(mode="json"))
                    changed.append(value)
            for execution_id in {value.execution_id for value in changed if value.execution_id is not None}:
                execution = self.require(connection, "executions", execution_id, Execution)
                if execution.status in {ExecutionStatus.WAITING_INPUT, ExecutionStatus.WAITING_APPROVAL}:
                    status = self.resumable_state(connection, execution)
                    self.change_state(
                        connection,
                        execution,
                        status,
                        reason="recovery_unknown" if status is ExecutionStatus.BLOCKED else None,
                    )
        return tuple(changed)

    def completion_reasons(
        self,
        connection: sqlite3.Connection,
        execution: Execution,
        report: Report,
        handled_input_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Check completion against persisted ownership, artifacts, and declared obligations."""
        reasons: list[str] = []
        latest_input = connection.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM inbox_items WHERE target_kind='execution' AND target_id=?",
            (execution.execution_id,),
        ).fetchone()[0]
        if report.input_watermark != latest_input:
            reasons.append("New accepted input has not been included in this completion candidate")
        if execution.control_intent != "none":
            reasons.append("A user pause or cancel request is pending")
        if execution.unknown_effects:
            reasons.append("External side effects remain unknown")
        if report.unresolved:
            reasons.append("The Worker reports unresolved obligations")
        if execution.delivery_contract.require_text and not report.summary.strip():
            reasons.append("The delivery contract requires a nonblank text result")
        if any(
            value.origin_input_id
            and value.target_execution_id == execution.execution_id
            and value.required
            and value.state == "owned"
            and value.input_id not in handled_input_ids
            for value in self.records(connection, "input_dispositions", InputDisposition)
        ):
            reasons.append("An important accepted input is still owned and unhandled")
        children = {
            value.execution_id: value
            for value in self.records(connection, "executions", Execution)
            if value.parent_execution_id == execution.execution_id
        }
        required_children = set(execution.delivery_contract.required_children) | {
            value.execution_id for value in children.values() if value.required
        }
        if any(
            identity not in children or children[identity].status is not ExecutionStatus.SUCCEEDED
            for identity in required_children
        ):
            reasons.append("A required child Execution has not succeeded")
        passed = {
            value.name
            for value in report.verification
            if value.producer == "host"
            and value.status == "passed"
            and value.evidence_refs
            and value.executed_at is not None
        }
        if not {value.name for value in execution.delivery_contract.required_verifications} <= passed:
            reasons.append("Required verification evidence is incomplete")
        required_artifacts = set(execution.delivery_contract.required_artifact_ids) | set(report.artifact_ids)
        for identity in required_artifacts:
            artifact = self.load(connection, "artifacts", identity, Artifact)
            if (
                artifact is None
                or artifact.owner_execution_id != execution.execution_id
                or artifact.deletion_state != "active"
            ):
                reasons.append(f"Artifact {identity} is unavailable or belongs to another Execution")
                continue
            path = (self.content_root / artifact.relative_path).resolve()
            if (
                not path.is_relative_to(self.content_root)
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != artifact.digest
            ):
                reasons.append(f"Artifact {identity} is missing or corrupt")
        return tuple(reasons)

    def accept_report(self, report: Report, *, handled_input_ids: tuple[str, ...] = ()) -> Report:
        """Commit a candidate and dispose inputs only after its actual delivery gate passes."""
        with self.transaction(write=True) as connection:
            return self.accept_report_in_transaction(connection, report, handled_input_ids=handled_input_ids)

    def accept_report_in_transaction(
        self, connection: sqlite3.Connection, report: Report, *, handled_input_ids: tuple[str, ...] = ()
    ) -> Report:
        """Apply one host-authored candidate within its existing product owner transaction."""
        digest = fingerprint({"report": report.model_dump(mode="json"), "handled_input_ids": list(handled_input_ids)})
        old = self.receipt(connection, f"report/{report.report_id}", digest, Report)
        if old is not None:
            return old
        execution = self.writable_execution(connection, report.execution_id)
        if execution.current_run_id != report.run_id:
            raise ProductConflict("The report does not belong to the current SDK Run")
        if execution.status not in {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLING}:
            raise ProductConflict("The Worker no longer owns active execution")
        self.save(connection, "reports", report.report_id, report, execution_id=report.execution_id)
        for item in self.records(connection, "inbox_items", InboxItem):
            if (
                item.target_kind == "execution"
                and item.target_id == execution.execution_id
                and item.source == "weave.validation"
                and item.target_sequence <= report.input_watermark
                and item.status != "handled"
            ):
                self.save_input(connection, item.model_copy(update={"status": "handled"}))
        reasons = report.rejection_reasons
        if report.kind == "ready":
            reasons = (*reasons, *self.completion_reasons(connection, execution, report, handled_input_ids))
        rejection = tuple(dict.fromkeys(reasons))
        for input_id in handled_input_ids:
            item = self.require(connection, "inbox_items", input_id, InboxItem)
            if (
                item.target_id != execution.execution_id
                or item.target_kind != "execution"
                or item.target_sequence > report.input_watermark
            ):
                raise ProductConflict("The report cannot dispose an input outside its owned range")
            disposition = self.require(
                connection,
                "input_dispositions",
                stable_id(f"disposition/{item.origin_input_id}/{execution.execution_id}"),
                InputDisposition,
            )
            if disposition.state != "owned":
                raise ProductConflict("The input is already disposed")
            if rejection:
                continue
            self.save_disposition(
                connection,
                disposition.model_copy(
                    update={
                        "state": "handled",
                        "revision": disposition.revision + 1,
                        "outcome_refs": (report.report_id,),
                    }
                ),
            )
            self.save_input(connection, item.model_copy(update={"status": "handled"}))
            self.activate_handback(connection, item.origin_input_id)
        value = report.model_copy(update={"rejection_reasons": rejection})
        self.save(connection, "reports", value.report_id, value, execution_id=value.execution_id)
        if report.kind == "ready" and not rejection:
            self.change_state(connection, execution, ExecutionStatus.SUCCEEDED, result=report.summary)
        elif report.kind == "needs_input" and not rejection:
            if report.question is None or not report.question.strip():
                raise ValueError("A needs_input report requires a concrete question")
            attention = Attention(
                attention_id=stable_id(f"report-attention/{report.report_id}"),
                execution_id=report.execution_id,
                kind="clarification",
                question=report.question,
            )
            self.save_attention(connection, attention)
            self.emit(connection, attention.attention_id, "attention.created", attention.model_dump(mode="json"))
            if execution.control_intent == "none":
                self.change_state(connection, execution, ExecutionStatus.WAITING_INPUT)
        elif report.kind == "failed" and not rejection:
            for disposition in self.records(connection, "input_dispositions", InputDisposition):
                if disposition.target_execution_id == execution.execution_id and disposition.state == "owned":
                    self.return_in_transaction(
                        connection,
                        disposition.input_id,
                        execution_id=execution.execution_id,
                        expected_revision=disposition.revision,
                        reason="The Worker failed before resolving this input",
                        effect_refs=(report.report_id,),
                        unknown_effects=bool(execution.unknown_effects),
                    )
            if execution.status is ExecutionStatus.CANCELLING:
                self.change_state(connection, execution, ExecutionStatus.BLOCKED, reason="unhandled_failure")
            else:
                self.change_state(connection, execution, ExecutionStatus.FAILED, result=report.summary)
        self.emit(
            connection,
            report.execution_id,
            "report.rejected" if rejection else "report.created",
            value.model_dump(mode="json"),
        )
        if report.kind in {"ready", "needs_input", "failed"} and not rejection:
            input_id = stable_id(f"report-input/{report.report_id}")
            self.enqueue(
                connection,
                input_id=input_id,
                origin_input_id=input_id,
                target_kind="router",
                target_id="router",
                content=value.model_dump_json(),
                source="weave.report",
                wake_policy="wake",
                kind="delegate_result",
                causation_id=report.report_id,
            )
        self.save_receipt(connection, f"report/{report.report_id}", digest, value)
        return value

    def queue_after_rejected_report(self, report_id: str, *, evidence_refs: tuple[str, ...]) -> Execution:
        """Continue a rejected delivery after its SDK Run has already succeeded.

        Args:
            report_id: Current persisted candidate with host validation failures.
            evidence_refs: Exact owned evidence identities available to the correction.

        Returns:
            The same Execution with a durable correction observation, or its terminal
            business failure when the automatic correction allowance is exhausted.

        Raises:
            ProductConflict: If the candidate is not the current terminal SDK success.
        """
        digest = fingerprint({"report_id": report_id, "evidence_refs": list(evidence_refs)})
        key = f"report/correction/{report_id}"
        with self.transaction(write=True) as connection:
            old = self.receipt(connection, key, digest, Execution)
            if old is not None:
                return old
            report = self.require(connection, "reports", report_id, Report)
            execution = self.writable_execution(connection, report.execution_id)
            mapping = self.require(
                connection, "execution_runs", f"{execution.execution_id}/{execution.run_attempt}", ExecutionRun
            )
            if (
                not report.rejection_reasons
                or execution.current_run_id != report.run_id
                or mapping.run_id != report.run_id
                or mapping.state != "succeeded"
            ):
                raise ProductConflict("Report correction requires the current committed successful SDK invocation")
            remaining = execution.budget_limit - execution.budget_consumed - execution.budget_reserved
            if execution.control_intent == "none" and (execution.validation_attempts >= 3 or remaining <= 0):
                self.accept_report_in_transaction(
                    connection,
                    Report(
                        report_id=stable_id(f"validation-failed/{report.run_id}"),
                        execution_id=execution.execution_id,
                        run_id=report.run_id,
                        input_watermark=report.input_watermark,
                        kind="failed",
                        summary=(
                            "The Worker could not produce a valid delivery within its correction limit or task budget. "
                            + " ".join(report.rejection_reasons)
                        ),
                        artifact_ids=report.artifact_ids,
                    ),
                )
                value = self.require(connection, "executions", execution.execution_id, Execution)
            else:
                if execution.control_intent != "cancel":
                    identity = stable_id(f"completion-correction/{report_id}")
                    pending = [
                        value.input_id
                        for value in self.records(connection, "input_dispositions", InputDisposition)
                        if value.target_execution_id == execution.execution_id and value.state == "owned"
                    ]
                    self.enqueue(
                        connection,
                        input_id=identity,
                        origin_input_id=identity,
                        target_kind="execution",
                        target_id=execution.execution_id,
                        content=json.dumps(
                            {
                                "kind": "weave.completion_rejected",
                                "report_id": report_id,
                                "reasons": list(report.rejection_reasons),
                                "evidence_refs": list(evidence_refs),
                                "pending_input_ids": pending,
                                "instruction": (
                                    "Review persisted results and pending inputs. Correct the delivery report. "
                                    "Reuse completed actions."
                                ),
                            }
                        ),
                        source="weave.validation",
                        kind="observation",
                        causation_id=report_id,
                        wake_policy="wake",
                    )
                target = self.resumable_state(connection, execution)
                value = execution.model_copy(
                    update={
                        "status": target,
                        "revision": execution.revision + 1,
                        "validation_attempts": (
                            execution.validation_attempts + 1
                            if execution.control_intent == "none"
                            else execution.validation_attempts
                        ),
                    }
                )
                self.save_execution(connection, value)
                self.emit(connection, value.execution_id, "execution.updated", value.model_dump(mode="json"))
            self.save_receipt(connection, key, digest, value)
            return value

    def reports(self, execution_id: str) -> tuple[Report, ...]:
        """Read committed report details without triggering Router inference."""
        with self.transaction() as connection:
            self.require(connection, "executions", execution_id, Execution)
            return tuple(
                value for value in self.records(connection, "reports", Report) if value.execution_id == execution_id
            )

    def settle_run_usage(self, execution_id: str, *, invocation_id: str, consumed: int | None) -> Execution:
        """Record actual cumulative SDK charges, including reported provider overage.

        Admission checks the original allowance separately. Already incurred cost
        remains payable even when a provider exceeds its requested output limit.
        """
        with self.transaction(write=True) as connection:
            execution = self.require(connection, "executions", execution_id, Execution)
            run = next(
                (
                    value
                    for value in self.records(connection, "execution_runs", ExecutionRun)
                    if value.execution_id == execution_id and value.invocation_id == invocation_id
                ),
                None,
            )
            if run is None:
                raise ProductConflict("Usage requires an accepted execution invocation")
            previous = run.charged_budget_output_tokens
            amount = (
                execution.budget_limit - execution.budget_consumed - execution.budget_reserved + previous
                if consumed is None
                else consumed
            )
            if amount < previous:
                raise ProductConflict("Cumulative invocation usage cannot move backwards")
            delta = amount - previous
            if delta == 0:
                return execution
            value = execution.model_copy(
                update={"budget_consumed": execution.budget_consumed + delta, "revision": execution.revision + 1}
            )
            self.save_execution(connection, value)
            charged = run.model_copy(update={"charged_budget_output_tokens": amount})
            self.save(
                connection,
                "execution_runs",
                f"{run.execution_id}/{run.attempt}",
                charged,
                execution_id=run.execution_id,
                invocation_id=run.invocation_id,
            )
            return value

    def validate_page(self, limit: int, cursor: int) -> None:
        """Reject unbounded page requests and malformed cursors."""
        if not 1 <= limit <= 100 or cursor < 0:
            raise ValueError("Page limits are 1..100 and cursors must be nonnegative")

    def page[T](self, values: tuple[T, ...], *, limit: int, cursor: int, sequence: int) -> Page[T]:
        """Bound an already selected ordered result in the current read transaction."""
        self.validate_page(limit, cursor)
        selected = values[cursor : cursor + limit]
        next_cursor = cursor + len(selected)
        return Page(
            items=selected,
            total=len(values),
            next_cursor=next_cursor if next_cursor < len(values) else None,
            read_sequence=sequence,
        )

    def reassignment_pending(self, connection: sqlite3.Connection, origin_input_id: str) -> bool:
        """Read remaining routing or clarification responsibility without rewriting handback history."""
        queued = connection.execute(
            "SELECT 1 FROM inbox_items WHERE origin_id=? AND target_kind='router' AND status!='handled' "
            "AND json_extract(payload,'$.source')='weave.handback' LIMIT 1",
            (origin_input_id,),
        ).fetchone()
        if queued is not None:
            return True
        return any(
            source.origin_input_id == origin_input_id
            for question in self.records(connection, "attentions", Attention)
            if question.execution_id is None and question.status == "pending"
            for source in self.routing_inputs(connection, question.source_input_ids)
        )

    def project_message(self, connection: sqlite3.Connection, message: Message) -> MessageView:
        """Join only the reports and artifact metadata actually referenced by this message."""
        reports = tuple(self.require(connection, "reports", identity, Report) for identity in message.report_ids)
        artifact_ids = dict.fromkeys(identity for report in reports for identity in report.artifact_ids)
        artifact_ids.update(
            dict.fromkeys(
                row["target_id"]
                for row in connection.execute(
                    "SELECT target_id FROM object_references "
                    "WHERE owner_kind='message' AND owner_id=? AND target_kind='artifact' ORDER BY target_id",
                    (message.message_id,),
                )
            )
        )
        artifacts = tuple(
            ArtifactMetadata.model_validate(
                self.require(connection, "artifacts", identity, Artifact).model_dump(exclude={"relative_path"})
            )
            for identity in artifact_ids
        )
        dispositions: list[InputDispositionView] = []
        for row in connection.execute(
            "SELECT payload FROM input_dispositions WHERE origin_id IN ("
            "SELECT origin_id FROM inbox_items WHERE target_kind='router' "
            "AND json_extract(payload,'$.causation_id')=?) ORDER BY rowid",
            (message.message_id,),
        ):
            disposition = InputDisposition.model_validate_json(row["payload"])
            owner = self.require(connection, "executions", disposition.target_execution_id, Execution)
            space = self.require(connection, "spaces", owner.space_id, Space)
            dispositions.append(
                InputDispositionView(
                    **disposition.model_dump(),
                    execution_objective=owner.objective,
                    space_name=space.name,
                    reassignment_pending=disposition.state == "returned"
                    and self.reassignment_pending(connection, disposition.origin_input_id),
                )
            )
        handbacks = tuple(
            Handback.model_validate_json(row["payload"])
            for row in connection.execute(
                "SELECT payload FROM input_handbacks WHERE origin_id IN ("
                "SELECT origin_id FROM inbox_items WHERE target_kind='router' "
                "AND json_extract(payload,'$.causation_id')=?) ORDER BY rowid",
                (message.message_id,),
            )
        )
        handback_resolutions = tuple(
            HandbackResolution.model_validate_json(row["payload"])
            for row in connection.execute(
                "SELECT r.payload FROM input_handback_resolutions r JOIN input_handbacks h ON h.id=r.id "
                "WHERE h.origin_id IN (SELECT origin_id FROM inbox_items WHERE target_kind='router' "
                "AND json_extract(payload,'$.causation_id')=?) ORDER BY r.rowid",
                (message.message_id,),
            )
        )
        return MessageView(
            **message.model_dump(),
            reports=tuple(
                ReportSummary.model_validate(value.model_dump(include=set(ReportSummary.model_fields)))
                for value in reports
            ),
            artifacts=artifacts,
            input_dispositions=tuple(dispositions),
            handbacks=handbacks,
            handback_resolutions=handback_resolutions,
        )

    def message_page(
        self, connection: sqlite3.Connection, *, before_sequence: int | None = None, limit: int = 50
    ) -> Page[MessageView]:
        """Project one SQL-bounded chat page at the caller's transaction watermark."""
        self.validate_page(limit, 0)
        parameters: tuple[int, ...]
        if before_sequence is None:
            query = "SELECT payload FROM chat_messages ORDER BY sequence DESC LIMIT ?"
            parameters = (limit + 1,)
        else:
            query = "SELECT payload FROM chat_messages WHERE sequence<? ORDER BY sequence DESC LIMIT ?"
            parameters = (before_sequence, limit + 1)
        rows = connection.execute(query, parameters).fetchall()
        selected = tuple(Message.model_validate_json(row["payload"]) for row in reversed(rows[:limit]))
        total = connection.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
        return Page(
            items=tuple(self.project_message(connection, value) for value in selected),
            total=total,
            next_cursor=selected[0].chat_sequence if len(rows) > limit else None,
            read_sequence=self.sequence(connection),
        )

    def messages(self, *, before_sequence: int | None = None, limit: int = 50) -> Page[MessageView]:
        """Read the permanent main chat using its own stable message sequence."""
        with self.transaction() as connection:
            return self.message_page(connection, before_sequence=before_sequence, limit=limit)

    def list_spaces(
        self, *, query: str = "", status: Literal["active", "archived"] | None = None, limit: int = 50, cursor: int = 0
    ) -> Page[SpaceView]:
        """Read bounded Spaces without treating absent list entries as deleted."""
        with self.transaction() as connection:
            values = tuple(
                self.space_view(connection, value)
                for value in self.records(connection, "spaces", Space)
                if not value.deleted
                and (status is None or value.status == status)
                and query.casefold() in f"{value.name} {value.purpose}".casefold()
            )
            return self.page(values, limit=limit, cursor=cursor, sequence=self.sequence(connection))

    def list_executions(
        self, *, space_id: str | None = None, status: ExecutionStatus | None = None, limit: int = 50, cursor: int = 0
    ) -> Page[ExecutionView]:
        """Read actual durable work independently of main-chat or SDK history."""
        with self.transaction() as connection:
            values = tuple(
                self.execution_view(connection, value)
                for value in self.records(connection, "executions", Execution)
                if (space_id is None or value.space_id == space_id)
                and (status is None or value.status is status)
                and not self.require(connection, "spaces", value.space_id, Space).deleted
            )
            return self.page(values, limit=limit, cursor=cursor, sequence=self.sequence(connection))

    def attentions(
        self,
        *,
        status: Literal["pending", "resolved", "expired", "withdrawn"] | None = "pending",
        limit: int = 50,
        cursor: int = 0,
    ) -> Page[AttentionView]:
        """Read valid decision facts using bounded product pagination."""
        with self.transaction() as connection:
            values = tuple(
                self.attention_view(connection, value)
                for value in self.records(connection, "attentions", Attention)
                if status is None or value.status == status
            )
            return self.page(values, limit=limit, cursor=cursor, sequence=self.sequence(connection))

    def probe_writable(self) -> None:
        """Verify the configured store can acquire and execute a write transaction."""
        with self.transaction(write=True) as connection:
            connection.execute("UPDATE spaces SET payload=payload WHERE 0")

    def events(self, *, after_sequence: int = 0, limit: int = 100) -> Page[ProductEvent]:
        """Replay globally ordered committed facts after a resumable cursor."""
        self.validate_page(limit, after_sequence)
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM product_events WHERE sequence>? ORDER BY sequence LIMIT ?", (after_sequence, limit + 1)
            ).fetchall()
            values = tuple(
                ProductEvent(
                    sequence=row["sequence"],
                    event_id=row["event_id"],
                    subject=row["subject"],
                    type=row["type"],
                    data=JSON_OBJECT.validate_json(row["data"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows[:limit]
            )
            total = connection.execute(
                "SELECT COUNT(*) FROM product_events WHERE sequence>?", (after_sequence,)
            ).fetchone()[0]
            return Page(
                items=values,
                total=total,
                next_cursor=values[-1].sequence if len(rows) > limit else None,
                read_sequence=self.sequence(connection),
            )

    def snapshot(self, *, limit: int = 50) -> Snapshot:
        """Build the first-screen aggregate from a single consistent read transaction."""
        self.validate_page(limit, 0)
        with self.transaction() as connection:
            sequence = self.sequence(connection)
            spaces = tuple(
                self.space_view(connection, value)
                for value in self.records(connection, "spaces", Space)
                if not value.deleted
            )
            executions = tuple(
                self.execution_view(connection, value)
                for value in self.records(connection, "executions", Execution)
                if value.status not in TERMINAL_STATUSES
                and not self.require(connection, "spaces", value.space_id, Space).deleted
            )
            attentions = tuple(
                self.attention_view(connection, value)
                for value in self.records(connection, "attentions", Attention)
                if value.status == "pending"
            )
            active_batches = connection.execute(
                "SELECT decision_id,attempt FROM route_batches WHERE state IN ('pending','candidate')"
            ).fetchall()
            active_input_ids = {
                input_id
                for row in active_batches
                for input_id in self.require(
                    connection, "route_attempts", f"{row['decision_id']}/{row['attempt']}", RouteAttempt
                ).source_input_ids
            }
            pending_inputs = tuple(
                value
                for value in self.records(connection, "inbox_items", InboxItem)
                if value.target_kind == "router" and value.status == "pending"
            )
            if any(value.wake_policy == "wake" for value in pending_inputs):
                active_input_ids.update(value.input_id for value in pending_inputs)
            return Snapshot(
                snapshot_sequence=sequence,
                routing=RoutingStatus(
                    state="processing" if active_input_ids else "idle",
                    pending_input_count=len(active_input_ids),
                ),
                messages=self.message_page(connection, limit=limit),
                spaces=self.page(spaces, limit=limit, cursor=0, sequence=sequence),
                executions=self.page(executions, limit=limit, cursor=0, sequence=sequence),
                attentions=self.page(attentions, limit=limit, cursor=0, sequence=sequence),
                capabilities={
                    "max_message_bytes": 65_536,
                    "max_attachment_bytes": 25 * 1024 * 1024,
                    "max_message_attachment_bytes": 100 * 1024 * 1024,
                    "max_attachments": 20,
                    "max_target_executions": MAX_TARGET_EXECUTIONS,
                    "max_context_refs": 20,
                    "queue_capacity": self.queue_capacity,
                    "tool_result_page_bytes": 8192,
                    "max_tool_result_page_bytes": 32768,
                },
            )
