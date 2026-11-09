import asyncio
from contextlib import aclosing, suppress
from uuid import uuid4

import httpx
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, RichLog, Static
from textual.worker import Worker, WorkerCancelled

from pleiad_weave.application.client import WeaveClient, display_json, execution_record
from pleiad_weave.infrastructure.storage.product_models import (
    AttentionView,
    ChatMessageRequest,
    Execution,
    ResolutionRequest,
)
from pleiad_weave.tui.state import TuiState


class EntityItem(ListItem):
    """Bind a workspace row to its stable product identity."""

    def __init__(self, identity: str, label: str, category: str, generation: int) -> None:
        super().__init__(Label(label, markup=False))
        self.identity = identity
        self.category = category
        self.generation = generation


class PleiadApp(App[None]):
    """Share permanent chat and explicit work controls with the running owner."""

    CSS_PATH = "styles/theme.tcss"
    TITLE = "Weave"
    BINDINGS = [  # noqa: RUF012
        ("ctrl+q", "quit", "Disconnect"),
        ("ctrl+r", "reconnect", "Refresh"),
        ("escape", "leave_answer", "Main chat"),
    ]

    def __init__(self, client: WeaveClient) -> None:
        super().__init__()
        self.client = client
        self.state = TuiState()
        self.event_worker: Worker[None] | None = None
        self.list_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Static("Weave  ·  permanent chat", id="heading")
        with Horizontal(id="workspace"):
            with Vertical(id="chat-pane"):
                yield Button("Earlier messages", id="older")
                yield RichLog(id="chat", wrap=True, markup=False, auto_scroll=False)
                yield Static("Main chat", id="composer-target")
                yield Input(placeholder="Message Weave", id="composer-input")
            with Vertical(id="details-pane"):
                with Horizontal(id="categories"):
                    yield Button("Work", id="executions")
                    yield Button("Attention", id="attentions")
                    yield Button("Spaces", id="spaces")
                yield ListView(id="entities")
                yield RichLog(id="detail", wrap=True, markup=False)
                with Horizontal(classes="actions"):
                    yield Button("Answer", id="answer", disabled=True)
                    yield Button("Approve", id="approve", disabled=True)
                    yield Button("Reject", id="reject", disabled=True)
                with Horizontal(classes="actions"):
                    yield Button("Pause", id="pause", disabled=True)
                    yield Button("Resume", id="resume", disabled=True)
                    yield Button("Cancel", id="cancel", disabled=True)
                    yield Button("Target", id="target", disabled=True)
        yield Static("Connecting", id="connection-status")
        yield Footer()

    async def on_mount(self) -> None:
        """Read shared state before enabling any mutation or event subscription."""
        await self.action_reconnect()
        self.query_one("#composer-input", Input).focus()

    async def on_unmount(self) -> None:
        """Detach this observer and leave all durable work with the service."""
        if self.event_worker is not None:
            self.event_worker.cancel()
            with suppress(WorkerCancelled):
                await self.event_worker.wait()
        await self.client.aclose()

    async def action_reconnect(self) -> None:
        """Verify current owner state and restart the product journal observer."""
        if self.event_worker is not None:
            self.event_worker.cancel()
            with suppress(WorkerCancelled):
                await self.event_worker.wait()
        try:
            await self.refresh_state()
        except (httpx.HTTPError, ValueError) as error:
            self.connection_failed(error)
            return
        self.event_worker = self.run_worker(self.observe(), name="product-journal", exclusive=True)

    async def refresh_state(self) -> None:
        """Apply only the latest snapshot generation and its current selected identity."""
        self.state.cache_generation += 1
        generation = self.state.cache_generation
        self.state.connected = False
        self.state.detail_stale = True
        self.set_status("Refreshing workspace facts · draft retained")
        self.sync_actions()
        try:
            snapshot = await self.client.snapshot()
        except (httpx.HTTPError, ValueError):
            if generation != self.state.cache_generation:
                return
            raise
        if generation != self.state.cache_generation:
            return
        self.state.apply_snapshot(snapshot)
        await self.render_entities()
        if generation != self.state.cache_generation:
            return
        try:
            await self.read_detail()
        except (httpx.HTTPError, ValueError) as error:
            self.set_status(f"Details unavailable · {error} · select another object or refresh")
        if generation != self.state.cache_generation:
            return
        self.render_chat()
        if not self.state.detail_stale:
            self.set_status("Connected to the workspace owner")
        self.sync_actions()

    async def observe(self) -> None:
        """Apply committed journal notifications without owning model work."""
        try:
            while True:
                async with aclosing(self.client.events(after_sequence=self.state.read_sequence)) as events:
                    async for event in events:
                        if event is not None and event.sequence <= self.state.read_sequence:
                            continue
                        await self.refresh_state()
                        if event is None:
                            break
                    else:
                        raise httpx.RemoteProtocolError("The product event subscription ended")
        except (httpx.HTTPError, ValueError) as error:
            self.connection_failed(error)

    def connection_failed(self, error: Exception) -> None:
        """Retain the draft while disabling mutations until an explicit fresh read."""
        self.state.connected = False
        self.set_status(f"Disconnected · {error} · Ctrl+R to refresh")
        self.sync_actions()

    def set_status(self, message: str) -> None:
        """Display connection or command feedback outside the main chat."""
        self.query_one("#connection-status", Static).update(message)

    def render_chat(self) -> None:
        """Render only formally committed product messages in chat order."""
        chat = self.query_one("#chat", RichLog)
        following = chat.is_vertical_scroll_end
        scroll_x, scroll_y = chat.scroll_x, chat.scroll_y
        chat.clear()
        for message in sorted(self.state.messages.values(), key=lambda value: value.chat_sequence):
            chat.write(f"{message.role}  ·  {message.chat_sequence}\n{message.body}\n")
        if following:
            self.call_after_refresh(chat.scroll_end, animate=False)
        else:
            self.call_after_refresh(chat.scroll_to, x=scroll_x, y=scroll_y, animate=False)

    async def render_entities(self) -> None:
        """Render one current list atomically while old rows retain their generation fence."""
        async with self.list_lock:
            listing = self.query_one("#entities", ListView)
            category = self.state.category
            generation = self.state.cache_generation
            await listing.clear()
            snapshot = self.state.snapshot
            if snapshot is None:
                return
            if self.state.category == "executions":
                rows = [
                    (value.execution_id, f"{value.status.value} · {value.objective}")
                    for value in snapshot.executions.items
                ]
            elif self.state.category == "attentions":
                rows = [(value.attention_id, f"{value.kind} · {value.question}") for value in snapshot.attentions.items]
            else:
                rows = [(value.space_id, value.name) for value in snapshot.spaces.items]
            if (category, generation) != (self.state.category, self.state.cache_generation):
                return
            await listing.extend(EntityItem(identity, label, category, generation) for identity, label in rows)
            for index, row in enumerate(rows):
                if row[0] == self.state.selected_id:
                    listing.index = index
                    break

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Inspect only rows from the current category and snapshot generation."""
        item = event.item
        if not isinstance(item, EntityItem) or (item.category, item.generation) != (
            self.state.category,
            self.state.cache_generation,
        ):
            return
        self.state.selection_version += 1
        self.state.selected_id = item.identity
        try:
            await self.read_detail()
        except (httpx.HTTPError, ValueError) as error:
            self.set_status(f"Details unavailable · {error} · select another object or refresh")
        self.sync_actions()

    def read_position(self) -> tuple[int, int, str, str | None]:
        """Identify the exact read generation and selection that may update the inspector."""
        return (self.state.cache_generation, self.state.selection_version, self.state.category, self.state.selected_id)

    async def read_detail(self) -> None:
        """Apply current details only while both navigation and read generation still match."""
        position = self.read_position()
        self.state.detail_stale = True
        self.state.detail = None
        self.state.execution = None
        self.state.attention = None
        self.sync_actions()
        detail = self.query_one("#detail", RichLog)
        detail.clear()
        identity = self.state.selected_id
        if identity is None:
            self.state.detail_stale = False
            detail.write("Select work, a Space, or an Attention to inspect its current facts.")
            return
        execution: Execution | None = None
        attention: AttentionView | None = None
        try:
            if self.state.category == "executions":
                value = await self.client.execution(identity)
                execution = execution_record(value)
            elif self.state.category == "attentions":
                attention = await self.client.attention(identity)
                value = attention.model_dump(mode="json")
            else:
                value = await self.client.get(f"/v1/spaces/{identity}")
        except (httpx.HTTPError, ValueError):
            if position != self.read_position():
                return
            raise
        if position != self.read_position():
            return
        self.state.detail, self.state.execution, self.state.attention = value, execution, attention
        self.state.detail_stale = False
        detail.write(display_json(value))

    def sync_actions(self) -> None:
        """Enable explicit controls only for verified current owner capabilities."""
        enabled = self.state.connected and not self.state.detail_stale and not self.state.submitting
        detail = self.state.detail
        actions = [] if detail is None else detail.get("available_actions", [])
        for action in ("pause", "resume", "cancel"):
            self.query_one(f"#{action}", Button).disabled = not (
                enabled and isinstance(actions, list) and action in actions
            )
        self.query_one("#target", Button).disabled = not (enabled and self.state.execution is not None)
        attention = self.state.attention
        pending = (
            enabled
            and attention is not None
            and attention.status == "pending"
            and "resolve" in attention.available_actions
        )
        approval = attention is not None and attention.kind == "approval"
        self.query_one("#answer", Button).disabled = not (pending and not approval)
        for action in ("approve", "reject"):
            self.query_one(f"#{action}", Button).disabled = not (pending and approval)
        self.query_one("#composer-input", Input).disabled = self.state.submitting
        self.query_one("#older", Button).disabled = self.state.snapshot is None or self.state.submitting

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dispatch one explicit workspace action without sending chat text implicitly."""
        action = event.button.id
        if action in {"executions", "attentions", "spaces"}:
            if action == "executions":
                self.state.category = "executions"
            elif action == "attentions":
                self.state.category = "attentions"
            else:
                self.state.category = "spaces"
            self.state.selected_id = None
            self.state.selection_version += 1
            await self.render_entities()
            await self.read_detail()
            self.sync_actions()
        elif action == "answer":
            self.state.answer_to = self.state.attention
            self.render_target()
            self.query_one("#composer-input", Input).focus()
        elif action == "target" and self.state.execution is not None:
            identity = self.state.execution.execution_id
            targets = self.state.target_execution_ids
            self.state.target_execution_ids = (
                tuple(value for value in targets if value != identity) if identity in targets else (*targets, identity)
            )
            self.render_target()
        elif action == "older":
            await self.load_older_messages()
        else:
            await self.apply_control(action)

    async def apply_control(self, action: str | None) -> None:
        """Commit a selected work command or exact approval through the owner."""
        if not self.state.connected or self.state.submitting:
            return
        self.state.submitting = True
        self.sync_actions()
        try:
            if action in {"approve", "reject"} and self.state.attention is not None:
                attention = self.state.attention
                request = ResolutionRequest(
                    request_id=str(uuid4()),
                    expected_revision=attention.revision,
                    decision="approve" if action == "approve" else "reject",
                    action_digest=attention.action_digest,
                )
                await self.client.resolve(attention.attention_id, request)
            elif self.state.execution is not None:
                if action == "pause":
                    await self.client.control(self.state.execution, "pause")
                elif action == "resume":
                    await self.client.control(self.state.execution, "resume")
                elif action == "cancel":
                    await self.client.control(self.state.execution, "cancel")
            await self.refresh_state()
        except (httpx.HTTPError, ValueError) as error:
            self.connection_failed(error)
        finally:
            self.state.submitting = False
            self.sync_actions()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit one main-chat message or atomic Attention answer with a stable retry key."""
        if not event.value.strip() or not self.state.connected or self.state.submitting:
            return
        self.state.submitting = True
        self.sync_actions()
        try:
            attention = self.state.answer_to
            if attention is None:
                message = self.state.pending_message
                if (
                    message is None
                    or message.text != event.value
                    or message.target_execution_ids != self.state.target_execution_ids
                ):
                    message = ChatMessageRequest(
                        client_message_id=str(uuid4()),
                        text=event.value,
                        target_execution_ids=self.state.target_execution_ids,
                    )
                    self.state.pending_message = message
                await self.client.submit(message)
            else:
                resolution = self.state.pending_resolution
                if (
                    resolution is None
                    or resolution.answer != event.value
                    or resolution.expected_revision != attention.revision
                    or self.state.pending_resolution_target != attention.attention_id
                ):
                    resolution = ResolutionRequest(
                        request_id=str(uuid4()), expected_revision=attention.revision, answer=event.value
                    )
                    self.state.pending_resolution = resolution
                    self.state.pending_resolution_target = attention.attention_id
                await self.client.resolve(attention.attention_id, resolution)
            event.input.value = ""
            self.state.pending_message = None
            self.state.pending_resolution = None
            self.state.pending_resolution_target = None
            self.state.answer_to = None
            self.render_target()
            await self.refresh_state()
        except (httpx.HTTPError, ValueError) as error:
            self.connection_failed(error)
        finally:
            self.state.submitting = False
            self.sync_actions()
            event.input.focus()

    async def load_older_messages(self) -> None:
        """Read older committed history while preserving the current draft and targets."""
        before = min((value.chat_sequence for value in self.state.messages.values()), default=None)
        generation = self.state.cache_generation
        try:
            page = await self.client.messages(before_sequence=before)
            if generation != self.state.cache_generation:
                return
            self.state.messages.update((value.message_id, value) for value in page.items)
            self.render_chat()
        except (httpx.HTTPError, ValueError) as error:
            self.connection_failed(error)

    def action_leave_answer(self) -> None:
        """Return the sole composer to main chat without changing its draft."""
        self.state.answer_to = None
        self.state.pending_resolution = None
        self.state.pending_resolution_target = None
        self.render_target()

    def render_target(self) -> None:
        """Show the exact command target beside the single composer."""
        if self.state.answer_to is not None:
            label = (
                f"Answer {self.state.answer_to.attention_id} · {self.state.answer_to.question}"
                " · Esc returns to main chat"
            )
        else:
            targets = ", ".join(self.state.target_execution_ids)
            label = "Main chat" if not targets else f"Main chat · target work {targets}"
        self.query_one("#composer-target", Static).update(label)
