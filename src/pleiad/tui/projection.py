"""Project Runtime Events into the TUI presentation state."""

from collections.abc import Mapping

from pleiad.runtime.events import RuntimeEvent
from pleiad.tui.state import (
    EdgeView,
    EventView,
    NodeView,
    TuiExecutionStatus,
    TuiState,
    event_data,
)


class RuntimeProjection:
    """Apply replayable Runtime Events idempotently to one TuiState."""

    def __init__(self, state: TuiState) -> None:
        self.state = state
        self._last_sequence: dict[str, int] = {}

    def apply(self, event: RuntimeEvent) -> bool:
        """Apply one event and return whether the state changed."""
        last = self._last_sequence.get(event.execution_id, 0)
        if event.sequence and event.sequence <= last:
            return False
        if event.sequence:
            self._last_sequence[event.execution_id] = event.sequence
        if self.state.execution_id not in {None, event.execution_id}:
            return False

        self.state.execution_id = event.execution_id
        data = event_data(event)
        self.state.latest_event = self._event_view(event, data)
        handler = getattr(self, f"_on_{event.event_type.replace('.', '_')}", None)
        if handler is not None:
            handler(event, data)
        return True

    def _on_execution_started(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event, data
        self.state.execution_status = TuiExecutionStatus.RUNNING
        self.state.input_status = "blocked"

    def _on_execution_completed(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event, data
        self.state.execution_status = TuiExecutionStatus.SUCCEEDED
        self.state.input_status = "idle"

    def _on_execution_failed(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event, data
        self.state.execution_status = TuiExecutionStatus.FAILED
        self.state.input_status = "idle"

    def _on_execution_cancelled(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event, data
        self.state.execution_status = TuiExecutionStatus.CANCELLED
        self.state.input_status = "idle"

    def _on_graph_revision_accepted(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event
        graph_id = data.get("graph_id")
        self.state.graph_id = graph_id if isinstance(graph_id, str) else None
        revision = data.get("revision")
        self.state.graph_revision = str(revision) if revision is not None else None
        self._load_graph(data)

    def _on_graph_started(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event
        graph_id = data.get("graph_id")
        if isinstance(graph_id, str):
            self.state.graph_id = graph_id
        revision = data.get("revision")
        self.state.graph_revision = (
            str(revision) if revision is not None else self.state.graph_revision
        )

    def _on_node_ready(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        self._upsert_node(event, data, "ready")

    def _on_node_started(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        self._upsert_node(event, data, "running")

    def _on_node_succeeded(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        self._upsert_node(event, data, "succeeded")

    def _on_node_completed(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        self._upsert_node(event, data, "succeeded")

    def _on_node_failed(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        self._upsert_node(event, data, "failed")

    def _on_graph_failed(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event, data
        self.state.execution_status = TuiExecutionStatus.FAILED

    def _on_graph_completed(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event, data

    def _on_agent_started(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event, data

    def _on_agent_response(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event
        run_id = _string(data.get("run_id"))
        if run_id:
            self.state.finish_thinking(run_id)

    def _on_agent_text_delta(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        del event
        delta = _string(data.get("delta"))
        if delta:
            self.state.append_pleiad_text(delta)

    def _on_agent_thinking_delta(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        run_id = _string(data.get("run_id")) or "unknown-run"
        delta = _string(data.get("delta"))
        if delta:
            self.state.append_thinking(delta, run_id=run_id, event_id=event.event_id)

    def _on_tool_started(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        tool_code = _string(data.get("tool_code")) or "unknown tool"
        self.state.start_tool(
            event_id=event.event_id,
            call_id=_string(data.get("call_id")),
            title=_string(data.get("tool_name")) or tool_code,
            tool_code=tool_code,
        )

    def _on_tool_progress(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        self.state.update_tool(
            event_id=event.event_id,
            call_id=_string(data.get("call_id")),
            status="running",
            detail=_detail(data),
        )

    def _on_tool_succeeded(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        self.state.update_tool(
            event_id=event.event_id,
            call_id=_string(data.get("call_id")),
            status="completed",
            detail=_elapsed_detail(data),
        )

    def _on_tool_failed(self, event: RuntimeEvent, data: dict[str, object]) -> None:
        self.state.update_tool(
            event_id=event.event_id,
            call_id=_string(data.get("call_id")),
            status="failed",
            detail=_detail(data) or _elapsed_detail(data),
        )

    def _load_graph(self, data: Mapping[str, object]) -> None:
        nodes = data.get("nodes")
        if isinstance(nodes, list):
            for raw in nodes:
                if isinstance(raw, Mapping):
                    node_id = _string(raw.get("node_id"))
                    if node_id:
                        self._ensure_node(
                            node_id,
                            name=node_id,
                            objective=_string(raw.get("objective")) or "",
                        )
        edges = data.get("edges")
        if isinstance(edges, list):
            self.state.edges = [
                EdgeView(
                    edge_id=_string(raw.get("edge_id")) or f"edge-{index}",
                    source_node_id=_string(raw.get("source_node_id")) or "",
                    target_node_id=_string(raw.get("target_node_id")) or "",
                )
                for index, raw in enumerate(edges, start=1)
                if isinstance(raw, Mapping)
            ]

    def _upsert_node(self, event: RuntimeEvent, data: Mapping[str, object], status: str) -> None:
        node_id = _string(data.get("node_id")) or "unknown-node"
        node = self._ensure_node(node_id, name=_string(data.get("node_name")) or node_id)
        node.status = status
        node.trace_id = event.trace_id
        node.span_id = event.span_id
        node.detail = _detail(data)
        error = data.get("error")
        if isinstance(error, Mapping):
            node.error_code = _string(error.get("code"))
            node.error_message = _string(error.get("message"))
        if status == "running":
            node.started_at = event.timestamp
        if status in {"succeeded", "failed", "cancelled"}:
            node.finished_at = event.timestamp

    def _ensure_node(self, node_id: str, *, name: str, objective: str = "") -> NodeView:
        node = self.state.nodes.get(node_id)
        if node is None:
            node = NodeView(node_id=node_id, name=name, objective=objective)
            self.state.nodes[node_id] = node
            self.state.node_order.append(node_id)
        elif name and node.name == node.node_id:
            node.name = name
        if objective and not node.objective:
            node.objective = objective
        return node

    def _event_view(self, event: RuntimeEvent, data: Mapping[str, object]) -> EventView:
        raw_status = data.get("status")
        status = _string(raw_status) or _event_status(event.event_type)
        elapsed = data.get("elapsed_ms")
        detail = _detail(data)
        if not detail:
            detail = _string(data.get("tool_name")) or _string(data.get("tool_code")) or ""
        return EventView(
            sequence=event.sequence,
            timestamp=event.timestamp,
            event_type=event.event_type,
            node_name=_string(data.get("node_name")) or _string(data.get("node_id")),
            status=status,
            detail=detail,
            elapsed_ms=int(elapsed) if isinstance(elapsed, int | float) else None,
            trace_id=event.trace_id,
            span_id=event.span_id,
        )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _detail(data: Mapping[str, object]) -> str:
    for key in ("detail", "message", "reason"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    error = data.get("error")
    if isinstance(error, Mapping):
        message = _string(error.get("message"))
        if message:
            return message
    return ""


def _elapsed_detail(data: Mapping[str, object]) -> str:
    elapsed = data.get("elapsed_ms")
    if isinstance(elapsed, int | float):
        return f"{elapsed:.0f} ms"
    return ""


def _event_status(event_type: str) -> str:
    if event_type.endswith("started"):
        return "running"
    if event_type.endswith("completed") or event_type.endswith("succeeded"):
        return "completed"
    if event_type.endswith("failed"):
        return "failed"
    if event_type.endswith("cancelled"):
        return "cancelled"
    return "in_progress"
