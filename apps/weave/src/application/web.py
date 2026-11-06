import asyncio
from collections.abc import AsyncGenerator
from typing import Literal
from uuid import UUID

from pleiad.contracts import ContractModel, JsonObject
from pleiad.errors.base import SystemFault
from pleiad.messages.message import AssistantMessage, ToolCall, ToolMessage, UserMessage

from pleiad_weave.agent.contracts import GraphSpec
from pleiad_weave.application.dto import EventResponse
from pleiad_weave.application.events import ExecutionEvent
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.storage.artifacts import JSON_OBJECT_ADAPTER
from pleiad_weave.infrastructure.storage.models import (
    ExecutionRecord,
    ExecutionStatus,
    GraphRunRecord,
    NodeRunRecord,
)

TERMINAL_STATUSES = frozenset({ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED})
TERMINAL_EVENTS = frozenset({"execution.completed", "execution.failed", "execution.cancelled"})


class WebArtifact(ContractModel):
    """Expose a node-owned JSON result without publishing filesystem locations."""

    artifact_id: str
    media_type: Literal["application/json"] = "application/json"
    node_run_id: str
    value: JsonObject


class ExecutionDetails(ContractModel):
    """Keep execution, graph definitions, attempts, and outputs as separate facts."""

    execution: ExecutionRecord
    graphs: tuple[GraphSpec, ...]
    graph_runs: tuple[GraphRunRecord, ...]
    node_runs: tuple[NodeRunRecord, ...]
    artifacts: tuple[WebArtifact, ...]


class ConversationMessage(ContractModel):
    """Project ordered SDK messages with their original Turn and call identities."""

    message_id: str
    turn_id: str
    turn_sequence: int
    agent_id: str
    turn_completed: bool
    role: Literal["user", "assistant", "tool"]
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    refusal: str | None = None


def execution_details(service: ExecutionService, execution_id: str) -> ExecutionDetails:
    """Read an execution and only graph records and artifacts belonging to it.

    Args:
        service: The running workspace's application service.
        execution_id: Durable product job identity.

    Returns:
        A read model suitable for the conversation's work canvas.

    Raises:
        LookupError: If the execution does not exist.
        SystemFault: If persisted graph or artifact data is missing or malformed.
    """
    execution = service.get(execution_id)
    if execution is None:
        raise LookupError("execution not found")
    runner = service.weave.graph_executor
    graph_runs = tuple(item for item in runner.graphs.list_records() if item.execution_id == execution_id)
    node_runs = tuple(item for item in runner.nodes.list_records() if item.execution_id == execution_id)
    graphs: dict[tuple[str, int], GraphSpec] = {}
    for event in service.repository.read_events(execution_id):
        if event.event_type == "graph.revision.accepted":
            graph = GraphSpec.model_validate(event.data)
            graphs[(graph.graph_id, graph.revision)] = graph
    references = {(item.graph_id, item.graph_revision) for item in graph_runs}
    references.update((item.graph_id, item.graph_revision) for item in node_runs)
    if execution.graph_id is not None and execution.graph_revision is not None:
        references.add((execution.graph_id, execution.graph_revision))
    for graph_id, revision in sorted(references):
        if (graph_id, revision) not in graphs:
            stored = service.weave.graph_store.revision(graph_id, revision)
            if stored is None:
                raise SystemFault("weave.graph_missing", "An execution's recorded graph revision is unavailable.")
            graphs[(graph_id, revision)] = stored.graph
    artifacts = tuple(read_node_artifact(service, item) for item in node_runs if item.artifact_id is not None)
    return ExecutionDetails(
        execution=execution,
        graphs=tuple(graphs.values()),
        graph_runs=tuple(sorted(graph_runs, key=lambda item: item.started_at)),
        node_runs=tuple(sorted(node_runs, key=lambda item: item.started_at)),
        artifacts=artifacts,
    )


def read_node_artifact(service: ExecutionService, node: NodeRunRecord) -> WebArtifact:
    """Read a persisted node's artifact by its generated identity, never a client path.

    Args:
        service: The owning workspace service.
        node: A validated node attempt selected for the requested execution.

    Returns:
        A JSON artifact and its node attempt association.

    Raises:
        SystemFault: If the reference escapes storage or its payload is unavailable.
    """
    artifact_id = node.artifact_id
    try:
        if artifact_id is None or UUID(artifact_id).hex != artifact_id:
            raise ValueError("Invalid generated artifact identity")
        root = service.weave.graph_executor.artifacts.root.resolve()
        path = root / f"{artifact_id}.json"
        if path.resolve().parent != root:
            raise ValueError("Artifact reference escapes its storage root")
        value = JSON_OBJECT_ADAPTER.validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise SystemFault("weave.artifact_unavailable", "The recorded node artifact cannot be read.") from error
    return WebArtifact(artifact_id=artifact_id, node_run_id=node.node_run_id, value=value)


async def conversation_messages(service: ExecutionService, conversation_id: str) -> tuple[ConversationMessage, ...]:
    """Read completed and active messages through the SDK's public memory boundary.

    Args:
        service: The owning workspace service.
        conversation_id: The exact conversation identity.

    Returns:
        Messages ordered by Turn sequence and their original declaration order.

    Raises:
        LookupError: If neither conversation metadata nor execution history exists.
    """
    conversation = await asyncio.to_thread(service.workspaces.get_conversation, conversation_id)
    if conversation is None:
        executions = await asyncio.to_thread(service.repository.list_records, conversation_id)
        if not executions:
            raise LookupError("conversation not found")
    memory = service.weave.memory.session(conversation_id)
    active = await memory.active()
    completed = await memory.read()
    turns = {turn.turn_id: turn for turn in completed}
    if active is not None and active.turn_id not in turns:
        turns[active.turn_id] = active
    messages: list[ConversationMessage] = []
    for turn in sorted(turns.values(), key=lambda item: item.sequence):
        for message in turn.messages:
            if not isinstance(message, UserMessage | AssistantMessage | ToolMessage):
                continue
            content = message.content
            if isinstance(content, tuple):
                content = "\n".join(block.text for block in content)
            messages.append(
                ConversationMessage(
                    message_id=message.message_id,
                    turn_id=turn.turn_id,
                    turn_sequence=turn.sequence,
                    agent_id=turn.agent_id,
                    turn_completed=turn.completed,
                    role=message.role,
                    content=content,
                    tool_calls=message.tool_calls if isinstance(message, AssistantMessage) else (),
                    tool_call_id=message.tool_call_id if isinstance(message, ToolMessage) else None,
                    refusal=message.refusal if isinstance(message, AssistantMessage) else None,
                )
            )
    return tuple(messages)


def event_frame(event: ExecutionEvent) -> str:
    """Encode an event with a replay cursor only when its sequence is durable."""
    identity = f"id: {event.sequence}\n" if event.sequence > 0 else ""
    return f"{identity}event: execution\ndata: {EventResponse.from_event(event).model_dump_json()}\n\n"


async def stream_execution_events(
    service: ExecutionService,
    execution_id: str,
    after_sequence: int = 0,
    *,
    queue_limit: int = 1024,
    heartbeat_seconds: float = 15.0,
) -> AsyncGenerator[str]:
    """Replay committed events and stream live progress until completion or disconnect.

    Subscribe before reading the journal so concurrent commits cannot fall into
    a replay/live gap. Live deltas have no durable cursor; reconnecting clients
    must reload the conversation snapshot. Overflow explicitly requests a
    reconnect instead of silently losing events. Closing this iterator always
    releases its subscription and never cancels the underlying execution.

    Args:
        service: The shared application service owning the execution.
        execution_id: Execution to observe.
        after_sequence: Exclusive journal replay cursor.
        queue_limit: Maximum buffered live events for this subscriber.
        heartbeat_seconds: Idle interval between SSE comments.

    Yields:
        Complete server-sent event frames or heartbeat comments.

    Raises:
        LookupError: If the execution does not exist.
        ValueError: If cursor or stream bounds are invalid.
    """
    if after_sequence < 0 or queue_limit < 1 or heartbeat_seconds <= 0:
        raise ValueError("Invalid execution stream bounds")
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[ExecutionEvent | None] = asyncio.Queue(maxsize=queue_limit)
    overflowed = False
    closed = False

    def enqueue(event: ExecutionEvent) -> None:
        nonlocal overflowed
        if closed or overflowed:
            return
        if queue.full():
            overflowed = True
            queue.get_nowait()
            queue.put_nowait(None)
        else:
            queue.put_nowait(event)

    def listener(event: ExecutionEvent) -> None:
        if event.execution_id == execution_id and not closed:
            loop.call_soon_threadsafe(enqueue, event)

    service.subscribe_events(listener)
    try:
        record = await asyncio.to_thread(service.get, execution_id)
        if record is None:
            raise LookupError("execution not found")
        cursor = after_sequence
        for event in await service.events(execution_id, after_sequence=cursor):
            cursor = event.sequence
            yield event_frame(event)
            if event.event_type in TERMINAL_EVENTS:
                return
        if record.status in TERMINAL_STATUSES:
            return
        while True:
            if overflowed:
                yield 'event: resync\ndata: {"reason":"buffer_overflow"}\n\n'
                return
            try:
                incoming = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if incoming is None:
                yield 'event: resync\ndata: {"reason":"buffer_overflow"}\n\n'
                return
            if incoming.sequence > 0:
                if incoming.sequence <= cursor:
                    continue
                for committed in await service.events(execution_id, after_sequence=cursor):
                    cursor = committed.sequence
                    yield event_frame(committed)
                    if committed.event_type in TERMINAL_EVENTS:
                        return
            else:
                yield event_frame(incoming)
    finally:
        closed = True
        service.unsubscribe_events(listener)
