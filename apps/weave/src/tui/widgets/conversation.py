from datetime import datetime

from rich.console import Group
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Click, Key
from textual.widget import Widget
from textual.widgets import Static

from pleiad_weave.tui.state import (
    ArchivedProcessGroupView,
    AssistantMessageView,
    ConversationItemView,
    ProcessItemView,
    SystemNoticeView,
    TuiState,
    UserMessageView,
)
from pleiad_weave.tui.widgets.process import ProcessPane


class ConversationPane(Vertical):
    """Render the primary conversation and its transient Process Dock."""

    def compose(self) -> ComposeResult:
        yield Static(id="conversation-header")
        yield Static(id="conversation-follow")
        yield ConversationViewport(id="conversation-viewport")
        yield ProcessPane(id="process-pane")

    def update_state(self, state: TuiState) -> None:
        """Update conversation items, follow hint, and live process state."""
        self.query_one("#conversation-header", Static).update(render_conversation_header(state))
        self.update_follow_hint(state)
        self.query_one(ConversationViewport).update_state(state)
        self.query_one(ProcessPane).update_state(state)

    def update_follow_hint(self, state: TuiState) -> None:
        """Show new-message feedback only while the user is reading older content."""
        hint = self.query_one("#conversation-follow", Static)
        if state.conversation_follow or state.conversation_unread == 0:
            hint.display = False
            return
        count = state.conversation_unread
        label = "new message" if count == 1 else "new messages"
        hint.update(Text(f"↓ {count} {label} · End to follow", style="#7FB7FF"))
        hint.display = True


class ConversationViewport(VerticalScroll):
    """Maintain independent message widgets and preserve the user's scroll position."""

    state: TuiState | None = None
    pending_items: tuple[ConversationItemView, ...] = ()
    sync_requested: bool = False
    sync_scheduled: bool = False
    sync_running: bool = False
    programmatic_scroll: bool = False

    def update_state(self, state: TuiState) -> None:
        """Schedule one coalesced synchronization of the conversation viewport."""
        self.state = state
        self.pending_items = tuple(state.conversation_items)
        self.sync_requested = True
        if not self.sync_running and not self.sync_scheduled:
            self.sync_scheduled = True
            self.call_after_refresh(self.start_sync)

    def start_sync(self) -> None:
        """Start the asynchronous DOM update after the current layout pass."""
        self.sync_scheduled = False
        if self.sync_running:
            return
        self.sync_running = True
        self.run_worker(self.rebuild(), name="conversation-render", exclusive=False)

    async def rebuild(self) -> None:
        """Synchronize conversation children while retaining follow or scroll state."""
        requested_items = self.pending_items
        state = self.state
        saved_scroll_y = self.scroll_y
        try:
            self.sync_requested = False
            await self.sync_children(requested_items)
            self.programmatic_scroll = True
            try:
                if state is None or state.conversation_follow:
                    self.scroll_end(animate=False, immediate=True)
                else:
                    self.scroll_to(y=saved_scroll_y, animate=False, immediate=True)
            finally:
                self.programmatic_scroll = False
        finally:
            self.sync_running = False
            if self.sync_requested and not self.sync_scheduled:
                self.sync_scheduled = True
                self.call_after_refresh(self.start_sync)

    async def sync_children(self, items: tuple[ConversationItemView, ...]) -> None:
        """Incrementally mount, update, and remove conversation items by stable ID."""
        requested_ids = {item.item_id for item in items}
        for widget in tuple(self.children):
            if widget.id == "conversation-empty":
                if items:
                    await widget.remove()
                continue
            if widget.id not in requested_ids:
                await widget.remove()

        if not items:
            if not any(widget.id == "conversation-empty" for widget in self.children):
                await self.mount(Static(render_empty_conversation(), id="conversation-empty"))
            return

        existing = {widget.id: widget for widget in self.children if widget.id is not None}
        new_widgets: list[Widget] = []
        for item in items:
            if item.item_id not in existing:
                new_widgets.append(build_conversation_widget(item))
                continue
            widget = existing[item.item_id]
            if not update_conversation_widget(widget, item):
                await widget.remove()
                new_widgets.append(build_conversation_widget(item))
        if new_widgets:
            await self.mount(*new_widgets)

    def watch_scroll_y(self, old_scroll_y: float, scroll_y: float) -> None:
        """Pause or resume conversation follow when the user scrolls."""
        super().watch_scroll_y(old_scroll_y, scroll_y)
        if self.programmatic_scroll or self.state is None:
            return
        following = self.max_scroll_y - scroll_y <= 1
        if following != self.state.conversation_follow:
            self.state.set_conversation_follow(following)
            parent = self.parent
            if isinstance(parent, ConversationPane):
                parent.update_follow_hint(self.state)


class UserMessage(Vertical):
    """Render one user message as plain text with a role gutter."""

    def __init__(self, view: UserMessageView) -> None:
        super().__init__(id=view.item_id, classes="conversation-item user-message")
        self.view = view

    def compose(self) -> ComposeResult:
        yield message_heading("▌ YOU", self.view.created_at, "#7FB7FF")
        yield Static(Text(self.view.content, style="#E0E4E8"), classes="message-body")

    def update_view(self, view: UserMessageView) -> None:
        """Update the plain-text body without replacing the widget."""
        self.view = view
        self.query_one(".message-body", Static).update(Text(view.content, style="#E0E4E8"))


class AssistantMessage(Vertical):
    """Render one formal WEAVE answer independently from process summaries."""

    def __init__(self, view: AssistantMessageView) -> None:
        super().__init__(id=view.item_id, classes="conversation-item assistant-message")
        self.view = view

    def compose(self) -> ComposeResult:
        yield message_heading("✦ WEAVE", self.view.created_at, "#5BD6E8")
        yield Static(render_assistant_body(self.view), classes="message-body")

    def update_view(self, view: AssistantMessageView) -> None:
        """Update the Markdown answer body while preserving focus and scroll state."""
        self.view = view
        self.query_one(".message-body", Static).update(render_assistant_body(view))


class ArchivedProcessGroup(Static):
    """Render one collapsed, user-readable Process Group in the conversation."""

    can_focus = True

    def __init__(self, view: ArchivedProcessGroupView) -> None:
        super().__init__(id=view.item_id, classes="conversation-item process-archive")
        self.view = view

    def on_mount(self) -> None:
        """Render the initial collapsed state."""
        self.update_content()

    def on_key(self, event: Key) -> None:
        """Toggle the archived detail on Enter or Space."""
        if event.key in {"enter", "space"}:
            self.toggle()
            event.stop()

    def on_click(self, event: Click) -> None:
        """Allow mouse users to expand the same Process Group."""
        self.toggle()
        event.stop()

    def toggle(self) -> None:
        """Switch between the compact summary and safe process details."""
        self.view.collapsed = not self.view.collapsed
        self.update_content()

    def update_content(self) -> None:
        """Refresh the summary and focus affordance."""
        self.update(render_archived_process(self.view))
        self.set_class(not self.view.collapsed, "expanded")


class SystemNotice(Vertical):
    """Render a short product notice without a traceback or raw exception."""

    def __init__(self, view: SystemNoticeView) -> None:
        super().__init__(id=view.item_id, classes=f"conversation-item system-notice {view.severity}")
        self.view = view

    def compose(self) -> ComposeResult:
        yield message_heading("! NOTICE", self.view.created_at, notice_color(self.view.severity))
        yield Static(Text(self.view.content, style="#E0E4E8"), classes="message-body")


def build_conversation_widget(item: ConversationItemView) -> Widget:
    """Build the widget for one typed conversation item."""
    if isinstance(item, UserMessageView):
        return UserMessage(item)
    if isinstance(item, AssistantMessageView):
        return AssistantMessage(item)
    if isinstance(item, ArchivedProcessGroupView):
        return ArchivedProcessGroup(item)
    return SystemNotice(item)


def update_conversation_widget(widget: Widget, item: ConversationItemView) -> bool:
    """Update a mounted conversation widget and report whether its type matches."""
    if isinstance(widget, UserMessage) and isinstance(item, UserMessageView):
        widget.update_view(item)
        return True
    if isinstance(widget, AssistantMessage) and isinstance(item, AssistantMessageView):
        widget.update_view(item)
        return True
    if isinstance(widget, ArchivedProcessGroup) and isinstance(item, ArchivedProcessGroupView):
        widget.view = item
        widget.update_content()
        return True
    return isinstance(widget, SystemNotice) and isinstance(item, SystemNoticeView)


def message_heading(label: str, created_at: datetime, color: str) -> Static:
    """Build a two-column role heading with a right-aligned local time."""
    heading = Table.grid(expand=True, padding=(0, 0))
    heading.add_column(ratio=1)
    heading.add_column(justify="right")
    timestamp = created_at.astimezone().strftime("%H:%M:%S")
    heading.add_row(Text(label, style=f"bold {color}"), Text(timestamp, style="#68727C"))
    return Static(heading, classes="message-heading")


def render_assistant_body(view: AssistantMessageView) -> Group | Markdown | Text:
    """Render Markdown answer content and a cursor only while it streams."""
    body = view.content or "Working…"
    if not view.content:
        return (
            Group(Text(body, style="#A0A9B2"), Text("▋", style="#7FB7FF"))
            if view.streaming
            else Text(body, style="#A0A9B2")
        )
    return Group(Markdown(body), Text("▋", style="#7FB7FF")) if view.streaming else Markdown(body)


def render_conversation_header(state: TuiState) -> Table:
    """Render the section label and count only for visible conversation messages."""
    header = Table.grid(expand=True, padding=(0, 0))
    header.add_column(ratio=1)
    header.add_column(justify="right")
    count = state.visible_message_count
    label = "message" if count == 1 else "messages"
    header.add_row(Text("CONVERSATION", style="bold #5BD6E8"), Text(f"{count} {label}", style="#68727C"))
    return header


def render_empty_conversation() -> Text:
    """Render the deliberately small initial state."""
    return Text("WEAVE is ready.\nDescribe an objective below.", style="#A0A9B2")


def render_archived_process(view: ArchivedProcessGroupView) -> Group:
    """Render a collapsed Process Group or its safe summaries when expanded."""
    marker = "▸" if view.collapsed else "▾"
    status_style = process_status_color(view.status)
    header = Text(f"{marker} {view.summary}", style=f"bold {status_style}")
    if view.collapsed:
        return Group(header)
    lines: list[Text] = [header]
    for item in view.items:
        lines.extend(render_process_item(item))
    return Group(*lines)


def render_process_item(item: ProcessItemView) -> list[Text]:
    """Render a bounded Process item without exposing provider payloads."""
    status_style = process_status_color(item.status)
    title = item.title
    if item.kind == "tool":
        code = f" · {item.tool_code}" if item.tool_code else ""
        line = Text(f"│ {title}{code}  {item.status.upper()}", style=status_style)
        details = [line]
        if item.detail:
            details.append(Text(f"└ {item.detail}", style="#A0A9B2"))
        return details
    return [Text(f"│ {item.content or title}", style="#A0A9B2")]


def process_status_color(status: str) -> str:
    """Return the semantic color for a process state."""
    return {
        "running": "#EAB308",
        "waiting approval": "#7FB7FF",
        "completed": "#56D364",
        "failed": "#EF6B73",
        "cancelled": "#EAB308",
    }.get(status, "#A0A9B2")


def notice_color(severity: str) -> str:
    """Return the semantic color for a system notice."""
    return {"error": "#EF6B73", "warning": "#EAB308"}.get(severity, "#5BD6E8")
