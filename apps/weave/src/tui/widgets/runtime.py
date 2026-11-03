from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from pleiad_weave.tui.state import LogRecordView, TuiState


class RuntimeView(Vertical):
    """Answer the Runtime question with phase, node, and Program Log only."""

    def compose(self) -> ComposeResult:
        yield Static(id="runtime-summary")
        yield ProgramLog(id="program-log")

    def update_state(self, state: TuiState) -> None:
        """Update the compact Runtime summary and independent log feed."""
        self.query_one("#runtime-summary", Static).update(render_runtime_summary(state))
        self.query_one(ProgramLog).update_state(state)


class ProgramLog(Vertical):
    """Render a bounded INFO+ log stream with follow and pause feedback."""

    state: TuiState | None = None
    rendered_record_ids: tuple[str, ...] | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="program-log-header")
        yield ProgramLogBuffer(id="program-log-buffer", min_width=1)

    def on_mount(self) -> None:
        """Connect the scroll buffer to this presentation controller."""
        self.query_one(ProgramLogBuffer).owner = self

    def update_state(self, state: TuiState) -> None:
        """Append new records without resetting the user's log scroll position."""
        self.state = state
        self.query_one("#program-log-header", Static).update(render_program_log_header(state))
        buffer = self.query_one(ProgramLogBuffer)
        buffer.owner = self
        records = state.runtime.log_records
        record_ids = tuple(record.record_id for record in records)
        if record_ids == self.rendered_record_ids:
            return
        saved_scroll_x = buffer.scroll_x
        saved_scroll_y = buffer.scroll_y
        previous_ids = self.rendered_record_ids
        if previous_ids and record_ids[: len(previous_ids)] == previous_ids:
            new_records = records[len(previous_ids) :]
            for record in new_records:
                buffer.write(render_log_record(record), scroll_end=False)
        else:
            buffer.clear()
            if records:
                for record in records:
                    buffer.write(render_log_record(record), scroll_end=False)
            else:
                buffer.write(Text("Waiting for program logs…", style="#68727C"), scroll_end=False)
        self.rendered_record_ids = record_ids
        buffer.programmatic_scroll = True
        try:
            if state.runtime.log_follow:
                buffer.scroll_end(animate=False, immediate=True, x_axis=False)
            else:
                buffer.scroll_to(x=saved_scroll_x, y=saved_scroll_y, animate=False, immediate=True)
        finally:
            buffer.programmatic_scroll = False

    def handle_follow_changed(self, follow: bool) -> None:
        """Update state and the header when the user reaches or leaves the tail."""
        if self.state is None or self.state.runtime.log_follow == follow:
            return
        self.state.set_program_log_follow(follow)
        self.query_one("#program-log-header", Static).update(render_program_log_header(self.state))


class ProgramLogBuffer(RichLog):
    """RichLog subclass that reports manual scrolling to its parent."""

    owner: ProgramLog | None = None
    programmatic_scroll: bool = False

    def watch_scroll_y(self, old_scroll_y: float, scroll_y: float) -> None:
        """Pause follow while the user reads above the newest record."""
        super().watch_scroll_y(old_scroll_y, scroll_y)
        if self.programmatic_scroll or self.owner is None:
            return
        self.owner.handle_follow_changed(self.max_scroll_y - scroll_y <= 1)


def render_runtime_summary(state: TuiState) -> Text:
    """Render only the phase and current node for the Runtime panel."""
    text = Text()
    phase = {
        "Idle": "等待任务",
        "Queued": "任务排队中",
        "Starting execution": "正在启动任务",
        "Waiting": "等待输入",
        "Resuming execution": "正在恢复任务",
        "Completed": "任务已完成",
        "Failed": "任务失败",
        "Cancelled": "任务已取消",
        "Graph ready": "任务图已就绪",
        "Executing graph": "正在执行任务图",
        "Graph complete": "任务图执行完成",
        "Graph failed": "任务图执行失败",
        "Executing node": "正在执行节点",
        "Analyzing request": "正在分析请求",
        "Writing answer": "正在生成回答",
        "Answer complete": "回答已生成",
        "Waiting for approval": "等待授权",
    }.get(state.runtime.phase, state.runtime.phase)
    if state.runtime.phase.startswith("Running "):
        phase = f"正在调用：{state.runtime.phase.removeprefix('Running ')}"
    text.append("当前任务\n", style="bold #5BD6E8")
    text.append("阶段  ", style="#68727C")
    text.append(f"{phase}\n", style="#E0E4E8")
    if state.runtime.current_node_name:
        text.append("节点  ", style="#68727C")
        text.append(state.runtime.current_node_name, style="#E0E4E8")
    return text


def render_program_log_header(state: TuiState) -> Text:
    """Render the Program Log label and its follow state."""
    text = Text()
    text.append("运行日志", style="bold #5BD6E8")
    if state.runtime.log_follow:
        text.append(" · 跟随最新", style="#68727C")
    else:
        text.append(f" · 已暂停 · 新增 {state.runtime.log_unread}", style="#EAB308")
    return text


def render_log_record(record: LogRecordView) -> Text:
    """Render one formatted record with timestamp and level on the first line."""
    level_style = {
        "WARNING": "#EAB308",
        "WARN": "#EAB308",
        "ERROR": "#EF6B73",
        "CRITICAL": "#EF6B73",
    }.get(record.level, "#56D364")
    text = Text()
    text.append(record.timestamp.astimezone().strftime("%H:%M:%S"), style="#68727C")
    text.append(" ")
    level = {"INFO": "信息", "WARNING": "警告", "WARN": "警告", "ERROR": "错误", "CRITICAL": "严重"}.get(
        record.level, record.level
    )
    label = Text(level, style=level_style)
    label.set_length(6)
    text.append(label)
    text.append("\n")
    text.append(record.message, style="#E0E4E8")
    return text
