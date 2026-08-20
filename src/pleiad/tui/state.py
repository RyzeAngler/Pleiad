"""Mutable presentation state used by the Pleiad terminal interface."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pleiad.runtime.events import RuntimeEvent
from pleiad.storage.models import ExecutionRecord


class TuiExecutionStatus(StrEnum):
    """Execution states visible in the main screen."""

    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class MessageView:
    """One renderable conversation message."""

    message_id: str
    role: str
    content: str
    created_at: datetime
    execution_id: str | None = None
    streaming: bool = False


@dataclass(slots=True)
class ProcessItemView:
    """One live thinking or Tool record shown in the fixed Thinking Process window."""

    item_id: str
    kind: str
    title: str
    status: str = "running"
    content: str = ""
    detail: str = ""
    run_id: str | None = None
    call_id: str | None = None
    event_id: str | None = None
    tool_code: str | None = None


@dataclass(slots=True)
class NodeView:
    """One renderable dynamic Graph node."""

    node_id: str
    name: str
    status: str = "pending"
    objective: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str = ""
    error_code: str | None = None
    error_message: str | None = None
    trace_id: str | None = None
    span_id: str | None = None

    @property
    def duration_ms(self) -> int | None:
        """Calculate a display duration when both timestamps are available."""
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))


@dataclass(frozen=True, slots=True)
class EdgeView:
    """One display-only Graph edge."""

    edge_id: str
    source_node_id: str
    target_node_id: str


@dataclass(slots=True)
class EventView:
    """One compact Runtime Event projection for Latest Event."""

    sequence: int
    timestamp: datetime
    event_type: str
    node_name: str | None
    status: str
    detail: str
    elapsed_ms: int | None
    trace_id: str | None
    span_id: str | None


@dataclass(slots=True)
class TuiState:
    """Presentation state independent from Runtime domain objects."""

    conversation_id: str = "default"
    execution_id: str | None = None
    model_label: str = "model: unavailable"
    execution_status: TuiExecutionStatus = TuiExecutionStatus.IDLE
    graph_id: str | None = None
    graph_revision: str | None = None
    logger_status: str = "on"
    trace_status: str = "on"
    input_status: str = "idle"
    messages: list[MessageView] = field(default_factory=list)
    process_items: list[ProcessItemView] = field(default_factory=list)
    nodes: dict[str, NodeView] = field(default_factory=dict)
    node_order: list[str] = field(default_factory=list)
    edges: list[EdgeView] = field(default_factory=list)
    latest_event: EventView | None = None
    selected_node_id: str | None = None

    @property
    def graph_node_count(self) -> int:
        """Return the number of nodes in the current Graph projection."""
        return len(self.node_order)

    @property
    def ordered_nodes(self) -> tuple[NodeView, ...]:
        """Return nodes in the order received from the Graph."""
        return tuple(self.nodes[node_id] for node_id in self.node_order if node_id in self.nodes)

    def begin_execution(self, conversation_id: str) -> None:
        """Start a new screen-level Execution projection."""
        self.conversation_id = conversation_id
        self.execution_id = None
        self.execution_status = TuiExecutionStatus.RUNNING
        self.graph_id = None
        self.graph_revision = None
        self.nodes.clear()
        self.node_order.clear()
        self.edges.clear()
        self.latest_event = None
        self.selected_node_id = None
        self.process_items.clear()
        self.input_status = "blocked"

    def add_user_message(self, content: str) -> None:
        """Append one User message to the visible conversation."""
        self.messages.append(
            MessageView(
                message_id=f"user-{len(self.messages) + 1}",
                role="user",
                content=content,
                created_at=datetime.now().astimezone(),
                execution_id=self.execution_id,
            )
        )

    def begin_pleiad_message(self) -> None:
        """Reserve a streaming Pleiad message for the current Execution."""
        self.messages.append(
            MessageView(
                message_id=f"pleiad-{len(self.messages) + 1}",
                role="pleiad",
                content="",
                created_at=datetime.now().astimezone(),
                execution_id=self.execution_id,
                streaming=True,
            )
        )

    def complete_pleiad_message(self, content: str) -> None:
        """Set the final Pleiad Markdown response."""
        for message in reversed(self.messages):
            if message.role == "pleiad" and message.streaming:
                message.content = content
                message.streaming = False
                return
        self.messages.append(
            MessageView(
                message_id=f"pleiad-{len(self.messages) + 1}",
                role="pleiad",
                content=content,
                created_at=datetime.now().astimezone(),
                execution_id=self.execution_id,
            )
        )

    def append_pleiad_text(self, delta: str) -> None:
        """Append one streamed visible text fragment to the active Pleiad message."""
        if not delta:
            return
        for message in reversed(self.messages):
            if message.role == "pleiad" and message.streaming:
                message.content += delta
                return
        self.messages.append(
            MessageView(
                message_id=f"pleiad-{len(self.messages) + 1}",
                role="pleiad",
                content=delta,
                created_at=datetime.now().astimezone(),
                execution_id=self.execution_id,
                streaming=True,
            )
        )

    def append_thinking(self, delta: str, *, run_id: str, event_id: str) -> None:
        """Append a streamed provider-exposed thinking fragment to the Thinking Process."""
        if not delta:
            return
        if self.process_items:
            latest = self.process_items[-1]
            if latest.kind == "thinking" and latest.run_id == run_id and latest.status == "running":
                latest.content += delta
                latest.event_id = event_id
                return
        self.process_items.append(
            ProcessItemView(
                item_id=f"thinking:{run_id}",
                kind="thinking",
                title="thinking",
                content=delta,
                run_id=run_id,
                event_id=event_id,
            )
        )
        self._trim_process_items()

    def finish_thinking(self, run_id: str) -> None:
        """Mark the current thinking segment complete when a response arrives."""
        for item in reversed(self.process_items):
            if item.kind == "thinking" and item.run_id == run_id and item.status == "running":
                item.status = "completed"
                return

    def start_tool(
        self,
        *,
        event_id: str,
        call_id: str | None,
        title: str,
        tool_code: str,
    ) -> None:
        """Add one live Tool execution to the fixed Thinking Process window."""
        self.process_items.append(
            ProcessItemView(
                item_id=call_id or event_id,
                kind="tool",
                title=title,
                tool_code=tool_code,
                call_id=call_id,
                event_id=event_id,
            )
        )
        self._trim_process_items()

    def update_tool(
        self,
        *,
        event_id: str,
        call_id: str | None,
        status: str,
        detail: str = "",
    ) -> None:
        """Update one Tool record using its call identifier."""
        for item in reversed(self.process_items):
            if item.kind != "tool":
                continue
            if (call_id and item.call_id == call_id) or (not call_id and item.event_id == event_id):
                item.status = status
                item.detail = detail or item.detail
                item.event_id = event_id
                return
        self.start_tool(
            event_id=event_id,
            call_id=call_id,
            title="tool",
            tool_code=detail,
        )
        self.process_items[-1].status = status

    def clear_process(self) -> None:
        """Clear only the transient Thinking Process window."""
        self.process_items.clear()

    def _trim_process_items(self) -> None:
        """Keep replayed process state bounded while the window remains fixed."""
        del self.process_items[:-200]

    def add_error_message(self, code: str | None, message: str | None) -> None:
        """Render a terminal error as a Pleiad message without a traceback."""
        text = "Execution did not complete."
        if code or message:
            detail = message or "An unexpected error occurred."
            text = f"{text}\n\n{code or 'SYSTEM_UNEXPECTED'} · {detail}"
        self.complete_pleiad_message(text)

    def apply_execution_record(self, record: ExecutionRecord) -> None:
        """Apply the durable terminal result after the live event stream ends."""
        self.execution_id = record.execution_id
        self.execution_status = TuiExecutionStatus(record.status.value)
        self.graph_id = record.graph_id
        self.graph_revision = str(record.graph_revision) if record.graph_revision else None
        self.input_status = "idle"
        if record.output:
            content = _output_text(record.output)
            if content:
                self.complete_pleiad_message(content)
        elif record.error_code or record.error:
            self.add_error_message(record.error_code, record.error)


def _output_text(output: dict[str, Any]) -> str:
    """Prefer text output and fall back to readable JSON."""
    text = output.get("text")
    if isinstance(text, str):
        return text
    if not output:
        return ""
    return json.dumps(output, ensure_ascii=False, indent=2, default=str)


def event_data(event: RuntimeEvent) -> dict[str, object]:
    """Copy a Runtime Event payload into a mutable presentation dictionary."""
    return dict(event.data)
