import sqlite3
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pleiad.contracts import ContractModel, JsonObject
from pleiad.orchestration.graph.state import GraphResult, GraphState, GraphStatus, NodeResult, NodeStatus
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.usage import RunUsage
from pydantic import AwareDatetime, Field

from pleiad_weave.agent.contracts import GraphSpec
from pleiad_weave.application.errors import ErrorRecord
from pleiad_weave.infrastructure.storage.product import ProductStore, fingerprint
from pleiad_weave.infrastructure.storage.product_models import Execution, Page, ProductConflict, Space


class GraphDefinition(ContractModel):
    """Persist an immutable graph revision owned by one real Execution."""

    execution_id: str
    graph: GraphSpec
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class GraphRunDetail(ContractModel):
    """Retain one graph invocation and its exact SDK checkpoint independently."""

    graph_run_id: str
    execution_id: str
    graph_id: str
    graph_revision: int
    requested_revision: int | None = None
    parent_run_id: str
    call_id: str
    input: JsonObject
    status: GraphStatus = GraphStatus.RUNNING
    revision: int = 1
    state: GraphState | None = None
    result: GraphResult | None = None
    error: ErrorRecord | None = None
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: AwareDatetime | None = None


class NodeAttempt(ContractModel):
    """Identify a node attempt, its SDK Session/Run, and owned output references."""

    node_attempt_id: str
    execution_id: str
    graph_run_id: str
    node_id: str
    attempt: int
    input: JsonObject
    status: NodeStatus = NodeStatus.RUNNING
    session_id: str | None = None
    sdk_run_id: str | None = None
    model_name: str | None = None
    usage: RunUsage | None = None
    checkpoint: RunCheckpoint | None = None
    result: NodeResult | None = None
    artifact_ids: tuple[str, ...] = ()
    error: ErrorRecord | None = None
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: AwareDatetime | None = None


class ToolResultDetail(ContractModel):
    """Persist an original model-visible tool observation under its source Run."""

    tool_result_id: str
    execution_id: str
    run_id: str
    step_id: str
    call_id: str
    text: str
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class TextPage(ContractModel):
    """Return a UTF-8 bounded page without exposing an absolute file path."""

    id: str
    text: str
    offset: int
    next_offset: int | None
    total_bytes: int
    read_sequence: int
    read_scope: Literal["product", "session"]


def text_page(
    identity: str,
    text: str,
    *,
    offset: int,
    max_bytes: int,
    read_sequence: int,
    read_scope: Literal["product", "session"],
) -> TextPage:
    """Read an immutable UTF-8 result with explicit byte and journal watermarks."""
    if offset < 0 or not 1 <= max_bytes <= 32768:
        raise ValueError("Invalid result page boundary")
    payload = text.encode("utf-8")
    if offset > len(payload):
        raise ValueError("Result page offset exceeds the original observation")
    try:
        payload[:offset].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Result page offset must begin at a UTF-8 boundary") from error
    chunk = payload[offset : offset + max_bytes]
    content = ""
    while chunk:
        try:
            content = chunk.decode("utf-8")
            break
        except UnicodeDecodeError as error:
            chunk = chunk[: error.start]
    if not chunk and offset < len(payload):
        raise ValueError("Result page byte limit cannot contain the next UTF-8 character")
    next_offset = offset + len(chunk)
    return TextPage(
        id=identity,
        text=content,
        offset=offset,
        next_offset=next_offset if next_offset < len(payload) else None,
        total_bytes=len(payload),
        read_sequence=read_sequence,
        read_scope=read_scope,
    )


class GraphRunPage(ContractModel):
    """Read one graph definition and its bounded attempt page at one product watermark."""

    graph_run: GraphRunDetail
    graph: GraphSpec
    node_attempts: Page[NodeAttempt]


class ProductDetails:
    """Own revisioned execution details in the common product transaction journal."""

    def __init__(self, store: ProductStore) -> None:
        """Use the same journal and ownership checks as product work."""
        self.store = store

    def require_execution(self, connection: sqlite3.Connection, execution_id: str) -> Execution:
        """Validate the requested owner before reading its detailed facts."""
        execution = self.store.require(connection, "executions", execution_id, Execution)
        if self.store.require(connection, "spaces", execution.space_id, Space).deleted:
            raise LookupError("The Execution's Space was deleted")
        return execution

    def definition(self, execution_id: str, graph_id: str, revision: int | None = None) -> GraphDefinition:
        """Read an explicit owned revision or the latest existing revision."""
        with self.store.transaction() as connection:
            self.require_execution(connection, execution_id)
            candidates = tuple(
                value
                for value in self.store.records(connection, "graph_definitions", GraphDefinition)
                if value.execution_id == execution_id
                and value.graph.graph_id == graph_id
                and (revision is None or value.graph.revision == revision)
            )
            if not candidates:
                raise LookupError(f"Graph revision not found: {graph_id}@{revision}")
            return max(candidates, key=lambda value: value.graph.revision)

    def submit(self, execution_id: str, graph: GraphSpec, *, expected_revision: int | None) -> GraphDefinition:
        """Validate compare-and-swap and store an immutable graph definition."""
        identity = f"{execution_id}/{graph.graph_id}/{graph.revision}"
        with self.store.transaction(write=True) as connection:
            self.store.writable_execution(connection, execution_id)
            old = self.store.load(connection, "graph_definitions", identity, GraphDefinition)
            if old is not None:
                if old.graph != graph:
                    raise ProductConflict("The graph revision already has a different definition")
                return old
            current = tuple(
                value.graph.revision
                for value in self.store.records(connection, "graph_definitions", GraphDefinition)
                if value.execution_id == execution_id and value.graph.graph_id == graph.graph_id
            )
            actual = max(current) if current else None
            if expected_revision != actual or graph.revision != (1 if actual is None else actual + 1):
                raise ProductConflict("Graph definition revision changed before submission", current_revision=actual)
            value = GraphDefinition(execution_id=execution_id, graph=graph)
            self.store.save(connection, "graph_definitions", identity, value)
            self.store.add_reference(connection, "graph", identity, "execution", execution_id)
            self.store.emit(connection, execution_id, "graph.definition.created", value.model_dump(mode="json"))
            return value

    def start_graph(
        self,
        execution_id: str,
        definition: GraphDefinition,
        *,
        parent_run_id: str,
        call_id: str,
        input_value: JsonObject,
        requested_revision: int | None = None,
    ) -> tuple[GraphRunDetail, bool]:
        """Idempotently link a tool invocation to one durable graph-run identity."""
        request: JsonObject = {
            "execution_id": execution_id,
            "graph_id": definition.graph.graph_id,
            "requested_revision": requested_revision,
            "parent_run_id": parent_run_id,
            "call_id": call_id,
            "input": input_value,
        }
        digest = fingerprint(request)
        key = f"graph/start/{execution_id}/{parent_run_id}/{call_id}"
        with self.store.transaction(write=True) as connection:
            self.store.writable_execution(connection, execution_id)
            if definition.execution_id != execution_id:
                raise LookupError("The graph definition belongs to another Execution")
            old = self.store.receipt(connection, key, digest, GraphRunDetail)
            if old is not None:
                return self.store.require(connection, "graph_runs", old.graph_run_id, GraphRunDetail), True
            record = GraphRunDetail(
                graph_run_id=uuid4().hex,
                execution_id=execution_id,
                graph_id=definition.graph.graph_id,
                graph_revision=definition.graph.revision,
                requested_revision=requested_revision,
                parent_run_id=parent_run_id,
                call_id=call_id,
                input=input_value,
            )
            self.store.save(connection, "graph_runs", record.graph_run_id, record)
            self.store.save_receipt(connection, key, digest, record)
            self.store.add_reference(connection, "graph_run", record.graph_run_id, "execution", execution_id)
            self.store.emit(connection, execution_id, "graph.run.created", record.model_dump(mode="json"))
            return record, False

    def graph_run_page(
        self, execution_id: str, graph_run_id: str, *, cursor: int = 0, limit: int = 100
    ) -> GraphRunPage:
        """Read graph metadata and a real attempt page within one ownership snapshot."""
        self.store.validate_page(limit, cursor)
        with self.store.transaction() as connection:
            self.require_execution(connection, execution_id)
            record = self.store.require(connection, "graph_runs", graph_run_id, GraphRunDetail)
            if record.execution_id != execution_id:
                raise LookupError("The GraphRun does not belong to this Execution")
            definition = self.store.require(
                connection,
                "graph_definitions",
                f"{execution_id}/{record.graph_id}/{record.graph_revision}",
                GraphDefinition,
            )
            values = tuple(
                value
                for value in self.store.records(connection, "node_attempts", NodeAttempt)
                if value.execution_id == execution_id and value.graph_run_id == graph_run_id
            )
            return GraphRunPage(
                graph_run=record,
                graph=definition.graph,
                node_attempts=self.store.page(
                    values, limit=limit, cursor=cursor, sequence=self.store.sequence(connection)
                ),
            )

    def graph_run(self, execution_id: str, graph_run_id: str) -> GraphRunDetail:
        """Read a graph run only through its actual owning Execution."""
        with self.store.transaction() as connection:
            self.require_execution(connection, execution_id)
            value = self.store.require(connection, "graph_runs", graph_run_id, GraphRunDetail)
            if value.execution_id != execution_id:
                raise LookupError("The GraphRun does not belong to this Execution")
            return value

    def save_graph(self, record: GraphRunDetail, *, expected_revision: int) -> GraphRunDetail:
        """Compare-and-swap a running or paused graph's committed SDK facts."""
        with self.store.transaction(write=True) as connection:
            self.store.writable_execution(connection, record.execution_id)
            current = self.store.require(connection, "graph_runs", record.graph_run_id, GraphRunDetail)
            if current.revision != expected_revision:
                raise ProductConflict("The GraphRun changed before its update", current_revision=current.revision)
            immutable = (
                "execution_id",
                "graph_id",
                "graph_revision",
                "requested_revision",
                "parent_run_id",
                "call_id",
                "input",
            )
            if any(getattr(current, field) != getattr(record, field) for field in immutable):
                raise ProductConflict("GraphRun ownership and original input cannot change")
            if (
                current.status in {GraphStatus.SUCCEEDED, GraphStatus.FAILED, GraphStatus.CANCELLED}
                and current != record
            ):
                raise ProductConflict("A terminal GraphRun cannot change")
            updated = record.model_copy(update={"revision": expected_revision + 1})
            self.store.save(connection, "graph_runs", record.graph_run_id, updated)
            self.store.emit(connection, record.execution_id, "graph.run.updated", updated.model_dump(mode="json"))
            return updated

    def begin_node(
        self,
        execution_id: str,
        graph_run_id: str,
        *,
        node_id: str,
        input_value: JsonObject,
        model_name: str | None,
        checkpoint: RunCheckpoint | None,
    ) -> NodeAttempt:
        """Create an attempt or reopen the same paused SDK invocation."""
        with self.store.transaction(write=True) as connection:
            self.store.writable_execution(connection, execution_id)
            graph = self.store.require(connection, "graph_runs", graph_run_id, GraphRunDetail)
            if graph.execution_id != execution_id:
                raise LookupError("The GraphRun does not belong to this Execution")
            attempts = tuple(
                value
                for value in self.store.records(connection, "node_attempts", NodeAttempt)
                if value.graph_run_id == graph_run_id and value.node_id == node_id
            )
            if checkpoint is not None:
                current = next((value for value in attempts if value.sdk_run_id == checkpoint.run_id), None)
                if (
                    current is None
                    or current.checkpoint != checkpoint
                    or current.input != input_value
                    or current.status is not NodeStatus.PAUSED
                ):
                    raise ProductConflict("The paused node attempt does not match its saved checkpoint")
                value = current.model_copy(update={"status": NodeStatus.RUNNING, "finished_at": None})
            else:
                if any(value.status in {NodeStatus.RUNNING, NodeStatus.PAUSED} for value in attempts):
                    raise ProductConflict("The node already has an unsettled attempt")
                value = NodeAttempt(
                    node_attempt_id=uuid4().hex,
                    execution_id=execution_id,
                    graph_run_id=graph_run_id,
                    node_id=node_id,
                    attempt=len(attempts) + 1,
                    input=input_value,
                    model_name=model_name,
                )
            self.store.save(connection, "node_attempts", value.node_attempt_id, value)
            self.store.add_reference(connection, "graph_node", value.node_attempt_id, "execution", execution_id)
            self.store.add_reference(connection, "node_attempt", value.node_attempt_id, "graph_run", graph_run_id)
            self.store.emit(connection, execution_id, "node.attempt.started", value.model_dump(mode="json"))
            return value

    def save_node(self, record: NodeAttempt) -> None:
        """Commit one real node attempt and its original output references."""
        with self.store.transaction(write=True) as connection:
            self.store.writable_execution(connection, record.execution_id)
            current = self.store.require(connection, "node_attempts", record.node_attempt_id, NodeAttempt)
            if (current.execution_id, current.graph_run_id, current.node_id, current.attempt) != (
                record.execution_id,
                record.graph_run_id,
                record.node_id,
                record.attempt,
            ):
                raise ProductConflict("Node attempt ownership cannot change")
            self.store.save(connection, "node_attempts", record.node_attempt_id, record)
            references = connection.execute(
                "SELECT owner_kind,owner_id FROM object_references WHERE target_kind='graph_node' AND target_id=?",
                (record.node_attempt_id,),
            ).fetchall()
            for reference in references:
                for artifact_id in record.artifact_ids:
                    self.store.add_reference(
                        connection, reference["owner_kind"], reference["owner_id"], "artifact", artifact_id
                    )
            self.store.emit(connection, record.execution_id, "node.attempt.updated", record.model_dump(mode="json"))

    def graph_runs(self, execution_id: str, *, cursor: int = 0, limit: int = 50) -> Page[GraphRunDetail]:
        """Read bounded graph invocations without executing any model."""
        if cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid graph-run page")
        with self.store.transaction() as connection:
            self.require_execution(connection, execution_id)
            values = tuple(
                value
                for value in self.store.records(connection, "graph_runs", GraphRunDetail)
                if value.execution_id == execution_id
            )
            return Page(
                items=values[cursor : cursor + limit],
                total=len(values),
                next_cursor=cursor + limit if cursor + limit < len(values) else None,
                read_sequence=self.store.sequence(connection),
            )

    def node_attempts(
        self, execution_id: str, graph_run_id: str, node_id: str | None = None, *, cursor: int = 0, limit: int = 50
    ) -> Page[NodeAttempt]:
        """Read actual attempts within a verified Execution and GraphRun boundary."""
        if cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid node-attempt page")
        self.graph_run(execution_id, graph_run_id)
        with self.store.transaction() as connection:
            values = tuple(
                value
                for value in self.store.records(connection, "node_attempts", NodeAttempt)
                if value.execution_id == execution_id
                and value.graph_run_id == graph_run_id
                and (node_id is None or value.node_id == node_id)
            )
            return Page(
                items=values[cursor : cursor + limit],
                total=len(values),
                next_cursor=cursor + limit if cursor + limit < len(values) else None,
                read_sequence=self.store.sequence(connection),
            )

    def node_attempt(self, execution_id: str, node_attempt_id: str) -> NodeAttempt:
        """Read one attempt after verifying its durable product owner."""
        with self.store.transaction() as connection:
            self.require_execution(connection, execution_id)
            value = self.store.require(connection, "node_attempts", node_attempt_id, NodeAttempt)
            if value.execution_id != execution_id:
                raise LookupError("The node attempt does not belong to this Execution")
            return value

    def save_tool_result(self, value: ToolResultDetail) -> None:
        """Retain the original observation under an immutable result identity."""
        with self.store.transaction(write=True) as connection:
            self.store.writable_execution(connection, value.execution_id)
            previous = self.store.load(connection, "tool_results", value.tool_result_id, ToolResultDetail)
            if previous is not None:
                if previous.model_dump(exclude={"created_at"}) != value.model_dump(exclude={"created_at"}):
                    raise ProductConflict("The tool result identity already has different content")
                return
            self.store.save(connection, "tool_results", value.tool_result_id, value)

    def tool_result(self, execution_id: str, tool_result_id: str) -> tuple[ToolResultDetail, int]:
        """Read an owned observation together with its product journal position."""
        with self.store.transaction() as connection:
            self.require_execution(connection, execution_id)
            value = self.store.require(connection, "tool_results", tool_result_id, ToolResultDetail)
            if value.execution_id != execution_id:
                raise LookupError("The tool result does not belong to this Execution")
            return value, self.store.sequence(connection)

    def tool_result_page(
        self, execution_id: str, tool_result_id: str, *, offset: int = 0, max_bytes: int = 8192
    ) -> TextPage:
        """Read an owned raw observation in UTF-8 pages bounded to 32 KiB."""
        value, sequence = self.tool_result(execution_id, tool_result_id)
        return text_page(
            tool_result_id,
            value.text,
            offset=offset,
            max_bytes=max_bytes,
            read_sequence=sequence,
            read_scope="product",
        )
