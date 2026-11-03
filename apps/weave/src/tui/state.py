from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pleiad_weave.application.events import ExecutionEvent
    from pleiad_weave.infrastructure.storage.models import ExecutionRecord


class TuiExecutionStatus(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RESUME_QUEUED = "resume_queued"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InputStatus(StrEnum):
    IDLE = "idle"
    SUBMITTING = "submitting"
    BLOCKED = "blocked"
    APPROVAL = "approval"
    ERROR = "error"


@dataclass(slots=True)
class UserMessageView:
    item_id: str
    content: str
    created_at: datetime
    execution_id: str | None = None


@dataclass(slots=True)
class AssistantMessageView:
    item_id: str
    content: str
    created_at: datetime
    execution_id: str | None = None
    streaming: bool = False


@dataclass(slots=True)
class SystemNoticeView:
    item_id: str
    content: str
    created_at: datetime
    severity: str = "information"


@dataclass(slots=True)
class ProcessItemView:
    item_id: str
    kind: str
    title: str
    status: str = "running"
    content: str = ""
    detail: str = ""
    turn_index: int | None = None
    call_id: str | None = None
    event_id: str | None = None
    tool_code: str | None = None


@dataclass(slots=True)
class ProcessGroupView:
    group_id: str
    started_at: datetime
    phase: str = "Submitting request"
    status: str = "running"
    items: list[ProcessItemView] = field(default_factory=list)
    finished_at: datetime | None = None

    @property
    def tool_count(self) -> int:
        """Return the number of tool calls in this process group."""
        return sum(item.kind == "tool" for item in self.items)

    @property
    def duration_ms(self) -> int | None:
        """Return the elapsed process time after the group has been archived."""
        if self.finished_at is None:
            return None
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))

    @property
    def summary(self) -> str:
        """Return the one-line collapsed representation for the conversation."""
        tools = f"{self.tool_count} tool" if self.tool_count == 1 else f"{self.tool_count} tools"
        duration = format_duration(self.duration_ms)
        return f"Process · {tools} · {duration}"


@dataclass(slots=True)
class ArchivedProcessGroupView:
    item_id: str
    group_id: str
    summary: str
    items: tuple[ProcessItemView, ...]
    created_at: datetime
    status: str
    collapsed: bool = True


ConversationItemView = UserMessageView | AssistantMessageView | ArchivedProcessGroupView | SystemNoticeView


@dataclass(slots=True)
class NodeView:
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
    edge_id: str
    source_node_id: str
    target_node_id: str


@dataclass(frozen=True, slots=True)
class LogRecordView:
    record_id: str
    timestamp: datetime
    level: str
    message: str
    logger: str
    execution_id: str | None = None
    turn_id: str | None = None
    tool_code: str | None = None


@dataclass(slots=True)
class RuntimeViewState:
    phase: str = "Idle"
    current_node_name: str | None = None
    log_records: list[LogRecordView] = field(default_factory=list)
    log_follow: bool = True
    log_unread: int = 0


@dataclass(frozen=True, slots=True)
class TraceEventView:
    sequence: int
    timestamp: datetime
    event_type: str
    detail: str
    status: str
    execution_id: str
    turn_index: int | None = None
    tool_name: str | None = None
    call_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None


@dataclass(slots=True)
class TraceViewState:
    events: list[TraceEventView] = field(default_factory=list)
    selected_sequence: int | None = None


@dataclass(slots=True)
class GraphViewState:
    graph_id: str | None = None
    revision: str | None = None
    nodes: dict[str, NodeView] = field(default_factory=dict)
    node_order: list[str] = field(default_factory=list)
    edges: list[EdgeView] = field(default_factory=list)
    selected_node_id: str | None = None

    @property
    def ordered_nodes(self) -> tuple[NodeView, ...]:
        """Return nodes in the deterministic order supplied by the Graph projection."""
        return tuple(self.nodes[node_id] for node_id in self.node_order if node_id in self.nodes)


@dataclass(slots=True)
class ModelContextView:
    model_name: str = "unavailable"
    used_context_tokens: int | None = None
    context_window_tokens: int | None = None


@dataclass(slots=True)
class TuiState:
    conversation_id: str = "default"
    execution_id: str | None = None
    execution_status: TuiExecutionStatus = TuiExecutionStatus.IDLE
    input_status: InputStatus = InputStatus.IDLE
    durable_record_applied: bool = False
    conversation_items: list[ConversationItemView] = field(default_factory=list)
    active_process: ProcessGroupView | None = None
    runtime: RuntimeViewState = field(default_factory=RuntimeViewState)
    graph: GraphViewState = field(default_factory=GraphViewState)
    trace: TraceViewState = field(default_factory=TraceViewState)
    model_context: ModelContextView = field(default_factory=ModelContextView)
    conversation_follow: bool = True
    conversation_unread: int = 0
    next_item_number: int = 1

    @property
    def visible_message_count(self) -> int:
        """Return the number of User and formal WEAVE messages."""
        return sum(isinstance(item, (UserMessageView, AssistantMessageView)) for item in self.conversation_items)

    @property
    def graph_node_count(self) -> int:
        """Return the number of nodes in the current Graph projection."""
        return len(self.graph.node_order)

    def begin_execution(self, conversation_id: str) -> None:
        """Reset per-execution projections and open a live Process Group."""
        self.conversation_id = conversation_id
        self.execution_id = None
        self.execution_status = TuiExecutionStatus.QUEUED
        self.input_status = InputStatus.SUBMITTING
        self.durable_record_applied = False
        self.graph = GraphViewState()
        self.trace = TraceViewState()
        self.runtime.phase = "Queued"
        self.runtime.current_node_name = None
        self.active_process = ProcessGroupView(
            group_id=self.new_item_id("process"),
            started_at=datetime.now().astimezone(),
            phase="Submitting request",
        )
        self.append_process_summary("Submitting request")

    def add_user_message(self, content: str) -> None:
        """Append one pure-text User message to the conversation."""
        self.append_conversation_item(
            UserMessageView(
                item_id=self.new_item_id("user"),
                content=content,
                created_at=datetime.now().astimezone(),
                execution_id=self.execution_id,
            )
        )

    def append_conversation_item(self, item: ConversationItemView) -> None:
        """Append a conversation item and track unread content while scrolled up."""
        self.conversation_items.append(item)
        if not self.conversation_follow:
            self.conversation_unread += 1

    def answer_for_execution(self) -> AssistantMessageView | None:
        """Return the latest formal answer belonging to the active execution."""
        return next(
            (
                item
                for item in reversed(self.conversation_items)
                if isinstance(item, AssistantMessageView) and item.execution_id == self.execution_id
            ),
            None,
        )

    def start_answer(self) -> AssistantMessageView:
        """Archive the live process and create the independent answer buffer."""
        if self.active_process is not None:
            self.archive_process("completed")
        answer = self.answer_for_execution()
        if answer is not None and answer.streaming:
            return answer
        answer = AssistantMessageView(
            item_id=self.new_item_id("weave"),
            content="",
            created_at=datetime.now().astimezone(),
            execution_id=self.execution_id,
            streaming=True,
        )
        self.append_conversation_item(answer)
        return answer

    def append_answer(self, delta: str) -> None:
        """Append a visible answer fragment to the formal answer buffer."""
        if not delta:
            return
        answer = self.start_answer()
        answer.content += delta

    def complete_answer(self, content: str | None = None) -> None:
        """Commit the formal answer without mixing it with process text."""
        answer = self.answer_for_execution()
        if answer is None:
            if not content:
                return
            answer = self.start_answer()
        if content:
            answer.content = content
        answer.streaming = False

    def finish_answer(self) -> None:
        """Remove the streaming cursor from an answer that has no final payload."""
        answer = self.answer_for_execution()
        if answer is not None and answer.streaming:
            answer.streaming = False

    def append_process_summary(self, summary: str) -> None:
        """Add a safe, user-readable process summary without exposing hidden reasoning."""
        if self.active_process is None:
            if self.answer_for_execution() is not None:
                return
            self.active_process = ProcessGroupView(
                group_id=self.new_item_id("process"),
                started_at=datetime.now().astimezone(),
            )
        self.active_process.phase = summary
        latest = self.active_process.items[-1] if self.active_process.items else None
        if latest is not None and latest.kind == "summary" and latest.content == summary:
            return
        self.active_process.items.append(
            ProcessItemView(
                item_id=self.new_item_id("step"),
                kind="summary",
                title=summary,
                content=summary,
            )
        )
        self.trim_process_items()

    def start_tool(
        self,
        *,
        event_id: str,
        call_id: str | None,
        title: str,
        tool_code: str,
        turn_index: int | None = None,
    ) -> None:
        """Add one safe Tool lifecycle item to the live Process Group."""
        if self.active_process is None:
            if self.answer_for_execution() is not None:
                return
            self.active_process = ProcessGroupView(
                group_id=self.new_item_id("process"),
                started_at=datetime.now().astimezone(),
            )
        self.active_process.phase = f"Running {title}"
        self.active_process.items.append(
            ProcessItemView(
                item_id=call_id or event_id,
                kind="tool",
                title=title,
                tool_code=tool_code,
                call_id=call_id,
                event_id=event_id,
                turn_index=turn_index,
            )
        )
        self.trim_process_items()

    def update_tool(
        self,
        *,
        event_id: str,
        call_id: str | None,
        status: str,
        detail: str = "",
    ) -> None:
        """Update one live Tool item using its stable call identifier."""
        if self.active_process is None:
            if self.answer_for_execution() is not None:
                return
            self.active_process = ProcessGroupView(
                group_id=self.new_item_id("process"),
                started_at=datetime.now().astimezone(),
            )
        for item in reversed(self.active_process.items):
            if item.kind != "tool":
                continue
            if (call_id and item.call_id == call_id) or (not call_id and item.event_id == event_id):
                item.status = status
                if detail:
                    item.detail = detail
                item.event_id = event_id
                return
        self.start_tool(event_id=event_id, call_id=call_id, title="tool", tool_code="unknown")
        if self.active_process.items:
            self.active_process.items[-1].status = status
            self.active_process.items[-1].detail = detail

    def archive_process(self, status: str = "completed") -> None:
        """Freeze the live Process Group as one collapsed conversation item."""
        process = self.active_process
        if process is None:
            return
        process.status = status
        process.finished_at = datetime.now().astimezone()
        self.append_conversation_item(
            ArchivedProcessGroupView(
                item_id=self.new_item_id("process-archive"),
                group_id=process.group_id,
                summary=process.summary,
                items=tuple(process.items),
                created_at=process.started_at,
                status=status,
            )
        )
        self.active_process = None

    def trim_process_items(self) -> None:
        """Bound process memory while retaining enough history for the archived group."""
        if self.active_process is not None:
            del self.active_process.items[:-200]

    def add_system_notice(self, content: str, severity: str = "information") -> None:
        """Append a concise user-facing system notice."""
        if self.active_process is not None:
            self.archive_process("failed" if severity == "error" else "cancelled")
        self.append_conversation_item(
            SystemNoticeView(
                item_id=self.new_item_id("notice"),
                content=content,
                created_at=datetime.now().astimezone(),
                severity=severity,
            )
        )

    def add_error_message(self, code: str | None, message: str | None) -> None:
        """Render a stable error code and safe summary without a traceback."""
        detail = message or "An unexpected error occurred."
        self.add_system_notice(
            f"Execution did not complete.\n\n{code or 'SYSTEM_UNEXPECTED'} · {detail}",
            severity="error",
        )

    def add_program_log(self, record: LogRecordView) -> None:
        """Append one already formatted program log record to the bounded feed."""
        self.runtime.log_records.append(record)
        del self.runtime.log_records[:-500]
        if not self.runtime.log_follow:
            self.runtime.log_unread += 1

    def set_program_log_follow(self, follow: bool) -> None:
        """Set the independent Program Log follow state."""
        self.runtime.log_follow = follow
        if follow:
            self.runtime.log_unread = 0

    def set_conversation_follow(self, follow: bool) -> None:
        """Set conversation follow state without changing the message projection."""
        self.conversation_follow = follow
        if follow:
            self.conversation_unread = 0

    def set_runtime_phase(self, phase: str, node_name: str | None = None) -> None:
        """Update the compact Runtime summary."""
        self.runtime.phase = phase
        self.runtime.current_node_name = node_name

    def update_model_context(
        self,
        *,
        model_name: str | None = None,
        used_context_tokens: int | None = None,
        context_window_tokens: int | None = None,
    ) -> None:
        """Update only model/context values explicitly supplied by the projection."""
        if model_name:
            self.model_context.model_name = model_name
        if used_context_tokens is not None and used_context_tokens >= 0:
            self.model_context.used_context_tokens = used_context_tokens
        if context_window_tokens is not None and context_window_tokens > 0:
            self.model_context.context_window_tokens = context_window_tokens

    def add_trace_event(self, event: TraceEventView) -> None:
        """Insert one ordered Agent Loop event and ignore duplicate sequences."""
        if any(item.sequence == event.sequence for item in self.trace.events):
            return
        self.trace.events.append(event)
        self.trace.events.sort(key=lambda item: item.sequence)

    def clear_visible_conversation(self) -> None:
        """Clear only visible conversation items while leaving active execution state intact."""
        self.conversation_items.clear()
        self.conversation_unread = 0

    def apply_execution_record(self, record: ExecutionRecord) -> None:
        """Apply durable state without treating approval waits as completion."""
        if self.durable_record_applied and self.execution_id == record.execution_id:
            return
        self.execution_id = record.execution_id
        self.execution_status = TuiExecutionStatus(record.status.value)
        terminal = self.execution_status in {
            TuiExecutionStatus.SUCCEEDED,
            TuiExecutionStatus.FAILED,
            TuiExecutionStatus.CANCELLED,
        }
        self.durable_record_applied = terminal
        self.input_status = InputStatus.IDLE if terminal else InputStatus.BLOCKED
        if record.graph_id:
            self.graph.graph_id = record.graph_id
        if record.graph_revision is not None:
            self.graph.revision = str(record.graph_revision)
        if self.execution_status is TuiExecutionStatus.WAITING_APPROVAL:
            self.input_status = InputStatus.APPROVAL
            self.set_runtime_phase("Waiting for approval · /approvals")
            return
        if self.execution_status is TuiExecutionStatus.RESUME_QUEUED:
            self.input_status = InputStatus.SUBMITTING
            self.set_runtime_phase("Approval saved · resuming")
            return
        if not terminal:
            return
        terminal_phase = {
            TuiExecutionStatus.SUCCEEDED: "Completed",
            TuiExecutionStatus.FAILED: "Failed",
            TuiExecutionStatus.CANCELLED: "Cancelled",
        }.get(self.execution_status, self.runtime.phase)
        self.set_runtime_phase(terminal_phase)
        if record.output:
            content = output_text(record.output)
            if content:
                self.complete_answer(content)
            else:
                self.finish_answer()
                self.archive_process("completed")
        elif self.execution_status is TuiExecutionStatus.CANCELLED:
            self.add_system_notice("Execution cancelled.", severity="warning")
        elif record.error is not None:
            self.add_error_message(record.error.code, record.error.message)
        elif self.execution_status is TuiExecutionStatus.FAILED:
            self.add_system_notice("Execution failed.", severity="error")
        else:
            self.finish_answer()
            self.archive_process("completed")

    def new_item_id(self, prefix: str) -> str:
        """Allocate a stable identifier for a visible presentation item."""
        item_id = f"{prefix}-{self.next_item_number}"
        self.next_item_number += 1
        return item_id


def format_duration(duration_ms: int | None) -> str:
    """Format a process duration using compact terminal-friendly units."""
    if duration_ms is None:
        return "…"
    return f"{duration_ms / 1000:.1f}s"


def output_text(output: Mapping[str, object]) -> str:
    """Prefer text output and fall back to readable JSON."""
    text = output.get("text")
    if isinstance(text, str):
        return text
    if not output:
        return ""
    return json.dumps(output, ensure_ascii=False, indent=2, default=str)


def event_data(event: ExecutionEvent) -> dict[str, object]:
    """Copy an Execution Event payload into a mutable presentation dictionary."""
    return dict(event.data)
