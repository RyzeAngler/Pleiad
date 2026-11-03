from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from pleiad_weave.tui.state import (
    EdgeView,
    InputStatus,
    NodeView,
    TraceEventView,
    TuiExecutionStatus,
    TuiState,
    event_data,
)

if TYPE_CHECKING:
    from pleiad_weave.application.events import ExecutionEvent

EventHandler = Callable[["ExecutionEvent", Mapping[str, object]], None]


class TuiProjection:
    """Project ordered Weave events into the separate TUI fact views."""

    def __init__(self, state: TuiState) -> None:
        self.state = state
        self.last_sequence_by_execution: dict[str, int] = {}
        self.seen_event_ids: set[str] = set()
        self.event_handlers: dict[str, EventHandler] = {
            "execution.queued": self.handle_execution_queued,
            "execution.started": self.handle_execution_started,
            "execution.waiting": self.handle_execution_waiting,
            "execution.waiting_approval": self.handle_execution_waiting,
            "execution.resume_queued": self.handle_execution_resume_queued,
            "execution.resumed": self.handle_execution_resumed,
            "execution.completed": self.handle_execution_completed,
            "execution.failed": self.handle_execution_failed,
            "execution.cancelled": self.handle_execution_cancelled,
            "graph.revision.accepted": self.handle_graph_revision_accepted,
            "graph.started": self.handle_graph_started,
            "graph.completed": self.handle_graph_completed,
            "graph.failed": self.handle_graph_failed,
            "graph.join.ready": self.handle_node_ready,
            "graph.node.started": self.handle_node_started,
            "graph.node.skipped": self.handle_node_skipped,
            "node.started": self.handle_node_started,
            "node.succeeded": self.handle_node_succeeded,
            "node.completed": self.handle_node_completed,
            "node.failed": self.handle_node_failed,
            "model.started": self.handle_turn_started,
            "model.completed": self.handle_turn_completed,
            "message.text.delta": self.handle_answer_delta,
            "turn.started": self.handle_turn_started,
            "turn.completed": self.handle_turn_completed,
            "tool.started": self.handle_tool_started,
            "tool.progress": self.handle_tool_progress,
            "tool.completed": self.handle_tool_completed,
            "tool.failed": self.handle_tool_failed,
            "tool.interrupted": self.handle_tool_approval_required,
            "tool.approval_required": self.handle_tool_approval_required,
            "process.started": self.handle_process_started,
            "process.summary.delta": self.handle_process_summary,
            "answer.started": self.handle_answer_started,
            "answer.delta": self.handle_answer_delta,
            "answer.completed": self.handle_answer_completed,
            "context.budget.checked": self.handle_context_update,
            "context.compacted": self.handle_context_update,
        }

    def apply(self, event: ExecutionEvent) -> bool:
        """Apply one event once and return whether it belongs to this execution."""
        if self.state.execution_id is not None and self.state.execution_id != event.execution_id:
            return False
        if event.event_id in self.seen_event_ids:
            return False
        if event.sequence:
            last_sequence = self.last_sequence_by_execution.get(event.execution_id, 0)
            if event.sequence <= last_sequence:
                return False
            self.last_sequence_by_execution[event.execution_id] = event.sequence
        self.seen_event_ids.add(event.event_id)
        self.state.execution_id = event.execution_id
        data = event_data(event)
        self.append_trace_event(event, data)
        handler = self.event_handlers.get(event.event_type)
        if handler is not None:
            handler(event, data)
        return True

    def handle_execution_queued(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.execution_status = TuiExecutionStatus.QUEUED
        self.state.input_status = InputStatus.SUBMITTING
        self.state.set_runtime_phase("Queued")
        self.state.append_process_summary("Queued")

    def handle_execution_started(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.execution_status = TuiExecutionStatus.RUNNING
        self.state.input_status = InputStatus.BLOCKED
        self.state.set_runtime_phase("Starting execution")
        self.state.append_process_summary("Starting execution")

    def handle_execution_waiting(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.execution_status = TuiExecutionStatus.WAITING_APPROVAL
        self.state.input_status = InputStatus.APPROVAL
        self.state.set_runtime_phase("Waiting for approval · /approvals")
        self.state.append_process_summary("Waiting for Tool approval")

    def handle_execution_resumed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.execution_status = TuiExecutionStatus.RUNNING
        self.state.input_status = InputStatus.BLOCKED
        self.state.set_runtime_phase("Resuming execution")
        self.state.append_process_summary("Resuming execution")

    def handle_execution_resume_queued(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        """Show the durable decision while its SDK continuation waits to run."""
        self.state.execution_status = TuiExecutionStatus.RESUME_QUEUED
        self.state.input_status = InputStatus.SUBMITTING
        self.state.set_runtime_phase("Approval saved · resuming")

    def handle_execution_completed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.execution_status = TuiExecutionStatus.SUCCEEDED
        self.state.input_status = InputStatus.IDLE
        self.state.set_runtime_phase("Completed")
        self.state.finish_answer()
        self.state.archive_process("completed")

    def handle_execution_failed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.execution_status = TuiExecutionStatus.FAILED
        self.state.input_status = InputStatus.IDLE
        self.state.set_runtime_phase("Failed")
        self.state.finish_answer()
        self.state.archive_process("failed")

    def handle_execution_cancelled(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.execution_status = TuiExecutionStatus.CANCELLED
        self.state.input_status = InputStatus.IDLE
        self.state.set_runtime_phase("Cancelled")
        self.state.finish_answer()
        self.state.archive_process("cancelled")

    def handle_graph_revision_accepted(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        graph_id = string_value(data.get("graph_id"))
        if graph_id:
            self.state.graph.graph_id = graph_id
        revision = data.get("revision")
        if revision is not None:
            self.state.graph.revision = str(revision)
        self.load_graph(data)
        self.state.set_runtime_phase("Graph ready")
        self.state.append_process_summary("Graph ready")

    def handle_graph_started(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.update_graph_identity(data)
        self.state.set_runtime_phase("Executing graph")
        self.state.append_process_summary("Executing graph")

    def handle_graph_completed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.set_runtime_phase("Graph complete")

    def handle_graph_failed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.set_runtime_phase("Graph failed")

    def handle_node_ready(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.upsert_node(event, data, "ready")

    def handle_node_started(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.upsert_node(event, data, "running")
        node_name = string_value(data.get("node_name")) or string_value(data.get("node_id")) or "node"
        self.state.set_runtime_phase("Executing node", node_name)
        self.state.append_process_summary(f"Executing {node_name}")

    def handle_node_succeeded(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.upsert_node(event, data, "succeeded")

    def handle_node_completed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.upsert_node(event, data, string_value(data.get("node_status")) or "succeeded")

    def handle_node_skipped(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.upsert_node(event, data, "skipped")

    def handle_node_failed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.upsert_node(event, data, "failed")

    def handle_turn_started(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.set_runtime_phase("Analyzing request")
        self.state.append_process_summary("Analyzing request")
        self.update_model_context(data)

    def handle_turn_completed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.append_process_summary("Model response received")
        self.update_model_context(data)

    def handle_thinking_delta(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.set_runtime_phase("Analyzing request")
        self.state.append_process_summary("Analyzing request")

    def handle_answer_started(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.start_answer()
        self.state.set_runtime_phase("Writing answer")

    def handle_answer_delta(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        delta = string_value(data.get("delta")) or string_value(data.get("text"))
        if delta:
            self.state.append_answer(delta)
            self.state.set_runtime_phase("Writing answer")

    def handle_answer_completed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        text = string_value(data.get("text"))
        self.state.complete_answer(text)
        self.state.set_runtime_phase("Answer complete")

    def handle_tool_started(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        tool_code = string_value(data.get("tool_code")) or "unknown"
        title = string_value(data.get("tool_name")) or tool_code
        self.state.start_tool(
            event_id=event.event_id,
            call_id=string_value(data.get("call_id")),
            title=title,
            tool_code=tool_code,
            turn_index=integer_value(data.get("turn_index")) or integer_value(data.get("turn")),
        )
        self.state.set_runtime_phase(f"Running {title}")

    def handle_tool_progress(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.update_tool(
            event_id=event.event_id,
            call_id=string_value(data.get("call_id")),
            status="running",
            detail=tool_detail(data),
        )

    def handle_tool_completed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        status = "completed" if data.get("ok", True) is not False else "failed"
        self.state.update_tool(
            event_id=event.event_id,
            call_id=string_value(data.get("call_id")),
            status=status,
            detail=tool_detail(data),
        )

    def handle_tool_failed(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.update_tool(
            event_id=event.event_id,
            call_id=string_value(data.get("call_id")),
            status="failed",
            detail=tool_detail(data),
        )

    def handle_tool_approval_required(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.update_tool(
            event_id=event.event_id,
            call_id=string_value(data.get("call_id")),
            status="waiting approval",
            detail="Waiting for approval",
        )
        self.state.set_runtime_phase("Waiting for approval")

    def handle_process_started(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.state.append_process_summary("Starting process")

    def handle_process_summary(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        summary = string_value(data.get("summary")) or string_value(data.get("detail"))
        if summary:
            self.state.append_process_summary(summary)

    def handle_context_update(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        self.update_model_context(data)

    def append_trace_event(self, event: ExecutionEvent, data: Mapping[str, object]) -> None:
        event_type = trace_event_type(event.event_type)
        if event.sequence <= 0 or event_type is None:
            return
        status = string_value(data.get("status")) or event_status(event_type)
        if event_type == "tool.completed" and data.get("ok") is False:
            status = "failed"
        self.state.add_trace_event(
            TraceEventView(
                sequence=event.sequence,
                timestamp=event.timestamp,
                event_type=event_type,
                detail=trace_detail(event_type, data),
                status=status,
                execution_id=event.execution_id,
                turn_index=integer_value(data.get("turn_index")) or integer_value(data.get("turn")),
                tool_name=string_value(data.get("tool_name")),
                call_id=string_value(data.get("call_id")),
                trace_id=event.trace_id,
                span_id=event.span_id,
            )
        )

    def update_graph_identity(self, data: Mapping[str, object]) -> None:
        graph_id = string_value(data.get("graph_id"))
        if graph_id:
            self.state.graph.graph_id = graph_id
        revision = data.get("revision")
        if revision is not None:
            self.state.graph.revision = str(revision)

    def load_graph(self, data: Mapping[str, object]) -> None:
        nodes = data.get("nodes")
        if isinstance(nodes, list):
            for raw in nodes:
                if isinstance(raw, Mapping):
                    node_id = string_value(raw.get("node_id"))
                    if node_id:
                        self.ensure_node(
                            node_id,
                            name=string_value(raw.get("node_name")) or node_id,
                            objective=string_value(raw.get("objective")) or "",
                        )
        edges = data.get("edges")
        if isinstance(edges, list):
            self.state.graph.edges = [
                EdgeView(
                    edge_id=string_value(raw.get("edge_id")) or f"edge-{index}",
                    source_node_id=string_value(raw.get("source_node_id")) or "",
                    target_node_id=string_value(raw.get("target_node_id")) or "",
                )
                for index, raw in enumerate(edges, start=1)
                if isinstance(raw, Mapping)
            ]

    def upsert_node(self, event: ExecutionEvent, data: Mapping[str, object], status: str) -> None:
        node_id = string_value(data.get("node_id")) or "unknown-node"
        node = self.ensure_node(node_id, name=string_value(data.get("node_name")) or node_id)
        node.status = status
        node.trace_id = event.trace_id
        node.span_id = event.span_id
        node.detail = node_detail(data)
        error = data.get("error")
        if isinstance(error, Mapping):
            node.error_code = string_value(error.get("code"))
            node.error_message = string_value(error.get("message"))
        if status == "running":
            node.started_at = event.timestamp
        if status in {"succeeded", "failed", "cancelled"}:
            node.finished_at = event.timestamp

    def ensure_node(self, node_id: str, *, name: str, objective: str = "") -> NodeView:
        node = self.state.graph.nodes.get(node_id)
        if node is None:
            node = NodeView(node_id=node_id, name=name, objective=objective)
            self.state.graph.nodes[node_id] = node
            self.state.graph.node_order.append(node_id)
        elif name and node.name == node.node_id:
            node.name = name
        if objective and not node.objective:
            node.objective = objective
        return node

    def update_model_context(self, data: Mapping[str, object]) -> None:
        self.state.update_model_context(
            model_name=string_value(data.get("model")),
            used_context_tokens=first_integer(
                data,
                ("used_context_tokens", "context_tokens", "input_tokens", "estimated_tokens"),
            ),
            context_window_tokens=first_integer(
                data,
                ("context_window_tokens", "model_window_tokens", "window_tokens"),
            ),
        )


def string_value(value: object) -> str | None:
    """Return a non-empty string field from an untrusted event payload."""
    return value if isinstance(value, str) and value else None


def integer_value(value: object) -> int | None:
    """Return a non-negative integer field without accepting booleans."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def first_integer(data: Mapping[str, object], keys: tuple[str, ...]) -> int | None:
    """Return the first valid integer from a white-listed set of fields."""
    for key in keys:
        value = integer_value(data.get(key))
        if value is not None:
            return value
    return None


def tool_detail(data: Mapping[str, object]) -> str:
    """Build a short safe Tool result detail from white-listed fields."""
    elapsed = integer_value(data.get("elapsed_ms"))
    error = data.get("error")
    error_code = string_value(error.get("code")) if isinstance(error, Mapping) else None
    if error_code:
        return error_code
    if elapsed is not None:
        return f"{elapsed} ms"
    if data.get("ok") is False:
        return "failed"
    return ""


def node_detail(data: Mapping[str, object]) -> str:
    """Build a short node detail without copying an arbitrary payload."""
    status = string_value(data.get("status"))
    error = data.get("error")
    error_code = string_value(error.get("code")) if isinstance(error, Mapping) else None
    return error_code or status or ""


def trace_event_type(event_type: str) -> str | None:
    """Map app and SDK lifecycle names to the Agent Loop Trace vocabulary."""
    primary = {
        "execution.queued",
        "execution.started",
        "execution.waiting",
        "execution.waiting_approval",
        "execution.resume_queued",
        "execution.resumed",
        "execution.completed",
        "execution.failed",
        "execution.cancelled",
        "run.started",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.timed_out",
        "run.limit_reached",
        "message.completed",
        "model.started",
        "model.completed",
        "model.failed",
        "turn.started",
        "turn.completed",
        "tool.started",
        "tool.approval_required",
        "tool.completed",
        "tool.failed",
        "handoff.completed",
        "worker.started",
        "worker.completed",
        "artifact.created",
    }
    if event_type in primary:
        return event_type
    return {
        "tool.interrupted": "tool.approval_required",
    }.get(event_type)


def trace_detail(event_type: str, data: Mapping[str, object]) -> str:
    """Render only white-listed Trace detail fields."""
    if event_type.startswith("turn."):
        turn = first_integer(data, ("turn_index", "turn"))
        tokens = first_integer(data, ("input_tokens", "used_context_tokens"))
        fields: list[str] = []
        if turn is not None:
            fields.append(f"第 {turn} 轮")
        if tokens is not None and event_type == "turn.completed":
            fields.append(f"输入 {tokens:,} token")
        return " · ".join(fields)
    if event_type.startswith("tool."):
        name = string_value(data.get("tool_name")) or string_value(data.get("tool_code"))
        fields = [] if name is None else [name]
        error = data.get("error")
        error_code = string_value(error.get("code")) if isinstance(error, Mapping) else None
        if error_code is not None:
            fields.append(error_code)
        elif data.get("ok") is False:
            fields.append("执行失败")
        elapsed = integer_value(data.get("elapsed_ms"))
        if elapsed is not None:
            fields.append(f"耗时 {elapsed} 毫秒")
        return " · ".join(fields)
    if event_type.startswith("execution."):
        error = data.get("error")
        if isinstance(error, Mapping):
            code = string_value(error.get("code"))
            return "" if code is None else code
        return ""
    for key in ("worker_id", "artifact_id", "agent_name"):
        value = string_value(data.get(key))
        if value:
            return "主助手" if value == "weave-supervisor" else value
    return ""


def event_status(event_type: str) -> str:
    """Return the text status associated with a lifecycle event name."""
    special = {
        "execution.queued": "queued",
        "execution.waiting": "waiting",
        "execution.waiting_approval": "waiting approval",
        "execution.resume_queued": "queued",
        "execution.resumed": "running",
        "run.timed_out": "timed_out",
        "run.limit_reached": "limit_reached",
        "artifact.created": "completed",
    }.get(event_type)
    if special is not None:
        return special
    if event_type.endswith("started"):
        return "running"
    if event_type.endswith("completed") or event_type.endswith("succeeded"):
        return "completed"
    if event_type.endswith("failed"):
        return "failed"
    if event_type.endswith("cancelled"):
        return "cancelled"
    if event_type.endswith("approval_required"):
        return "waiting approval"
    return "in progress"
