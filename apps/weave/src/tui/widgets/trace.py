from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from pleiad_weave.tui.state import TraceEventView, TuiState


class TraceView(Vertical):
    """Render ordered execution events in stable, labelled columns."""

    rendered_events: tuple[TraceEventView, ...] = ()

    def compose(self) -> ComposeResult:
        yield Static(id="trace-header")
        yield DataTable[Text](id="trace-log", cursor_type="row", show_row_labels=False, zebra_stripes=True)

    def on_mount(self) -> None:
        """Set column widths in terminal cells, including double-width Chinese text."""
        table = self.query_one(DataTable)
        table.add_column("事件", width=14)
        table.add_column("状态", width=8)
        table.add_column("时间", width=8)
        table.add_column("序号", width=4)
        table.add_column("说明")

    def update_state(self, state: TuiState) -> None:
        """Append events while preserving selection and both scroll positions."""
        self.query_one("#trace-header", Static).update(render_trace_header(state))
        events = tuple(state.trace.events)
        if events == self.rendered_events:
            return
        table: DataTable[Text] = self.query_one(DataTable)
        follow_tail = table.scroll_y >= table.max_scroll_y - 1
        saved_scroll_x, saved_scroll_y = table.scroll_x, table.scroll_y
        if self.rendered_events and events[: len(self.rendered_events)] == self.rendered_events:
            new_events = events[len(self.rendered_events) :]
        else:
            table.clear()
            new_events = events
        for event in new_events:
            table.add_row(*render_trace_event(event), key=str(event.sequence))
        self.rendered_events = events
        if follow_tail:
            table.scroll_end(animate=False, x_axis=False)
        else:
            table.scroll_to(x=saved_scroll_x, y=saved_scroll_y, animate=False)


def render_trace_header(state: TuiState) -> Text:
    """Render the number of persisted execution events."""
    return Text(f"执行事件 · {len(state.trace.events)} 条", style="bold #5BD6E8")


def render_trace_event(event: TraceEventView) -> tuple[Text, ...]:
    """Explain event names and outcomes without changing stored event identities."""
    event_label = {
        "execution.queued": "任务排队",
        "execution.started": "任务启动",
        "execution.waiting": "任务等待输入",
        "execution.waiting_approval": "任务等待审批",
        "execution.resume_queued": "任务恢复排队",
        "execution.resumed": "任务恢复",
        "execution.completed": "任务结束",
        "execution.failed": "任务失败",
        "execution.cancelled": "任务取消",
        "run.started": "智能体启动",
        "run.completed": "智能体结束",
        "run.failed": "智能体失败",
        "run.cancelled": "智能体取消",
        "run.timed_out": "智能体超时",
        "run.limit_reached": "运行达到限额",
        "message.completed": "消息接收完成",
        "model.started": "请求模型",
        "model.completed": "模型响应结束",
        "model.failed": "模型请求失败",
        "turn.started": "开始新一轮",
        "turn.completed": "本轮结束",
        "tool.started": "调用工具",
        "tool.approval_required": "工具等待授权",
        "tool.completed": "工具返回结果",
        "tool.failed": "工具执行失败",
        "handoff.completed": "智能体交接",
        "worker.started": "子任务启动",
        "worker.completed": "子任务结束",
        "artifact.created": "产物已保存",
    }.get(event.event_type)
    status_label = {
        "queued": "排队中",
        "running": "执行中",
        "in progress": "处理中",
        "waiting": "等待输入",
        "waiting approval": "等待授权",
        "completed": "完成",
        "succeeded": "成功",
        "failed": "失败",
        "cancelled": "已取消",
        "timed_out": "超时",
        "limit_reached": "达到限额",
    }.get(event.status, f"未知：{event.status}")
    status_style = {
        "running": "#EAB308",
        "waiting approval": "#7FB7FF",
        "failed": "#EF6B73",
        "timed_out": "#EF6B73",
        "limit_reached": "#EF6B73",
        "cancelled": "#EAB308",
        "completed": "#56D364",
        "succeeded": "#56D364",
    }.get(event.status, "#A0A9B2")
    detail = event.detail
    if event_label is None:
        detail = f"{event.event_type} · {detail}" if detail else event.event_type
    return (
        Text("其他事件" if event_label is None else event_label, style="#E0E4E8"),
        Text(status_label, style=status_style),
        Text(event.timestamp.astimezone().strftime("%H:%M:%S"), style="#68727C"),
        Text(str(event.sequence), style="#68727C", justify="right"),
        Text(detail, style="#A0A9B2"),
    )
