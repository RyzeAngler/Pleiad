import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
from pleiad.contracts import JsonObject

from pleiad_weave.application.client import JSON_OBJECT
from pleiad_weave.infrastructure.storage.product_models import (
    Attention,
    AttentionView,
    Execution,
    ExecutionStatus,
    ExecutionView,
    Message,
    MessageReceipt,
    MessageView,
    Page,
    RoutingStatus,
    Snapshot,
    Space,
    SpaceView,
)


class JournalStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.records: asyncio.Queue[bytes] = asyncio.Queue()
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        try:
            while True:
                yield await self.records.get()
        finally:
            self.closed.set()

    async def aclose(self) -> None:
        self.closed.set()


@dataclass(slots=True)
class HttpOwner:
    messages: list[Message] = field(default_factory=list)
    space: Space = field(default_factory=lambda: Space(space_id="space", name="Research"))
    attention: Attention = field(
        default_factory=lambda: Attention(
            attention_id="question", execution_id="job", kind="clarification", question="Which source?"
        )
    )
    execution: Execution = field(init=False)
    requests: list[tuple[str, str, JsonObject | None]] = field(default_factory=list)
    journal: JournalStream = field(default_factory=JournalStream)
    sequence: int = 1
    reject_submit: bool = False
    available_actions: tuple[str, ...] = ("pause", "cancel")

    def __post_init__(self) -> None:
        self.execution = Execution(
            execution_id="job",
            session_id="worker",
            space_id="space",
            objective="Research the topic",
            config_snapshot=self.space,
            status=ExecutionStatus.RUNNING,
        )

    def snapshot(self) -> Snapshot:
        return Snapshot(
            snapshot_sequence=self.sequence,
            routing=RoutingStatus(state="idle", pending_input_count=0),
            messages=Page(
                items=tuple(MessageView.model_validate(value.model_dump()) for value in self.messages),
                total=len(self.messages),
                next_cursor=None,
                read_sequence=self.sequence,
            ),
            spaces=Page(
                items=(
                    SpaceView(
                        **self.space.model_dump(),
                        available_actions=("update",),
                        disabled_reasons={},
                        active_execution_count=1,
                        pending_attention_count=1,
                    ),
                ),
                total=1,
                next_cursor=None,
                read_sequence=self.sequence,
            ),
            executions=Page(
                items=(
                    ExecutionView(
                        **self.execution.model_dump(),
                        available_actions=self.available_actions,
                        disabled_reasons={},
                        pending_prerequisites=(),
                        pause_requested=False,
                    ),
                ),
                total=1,
                next_cursor=None,
                read_sequence=self.sequence,
            ),
            attentions=Page(
                items=(
                    AttentionView(
                        **self.attention.model_dump(),
                        execution_objective=self.execution.objective,
                        space_name=self.space.name,
                        available_actions=("resolve",),
                        disabled_reasons={},
                    ),
                ),
                total=1,
                next_cursor=None,
                read_sequence=self.sequence,
            ),
            capabilities={"csrf_token": "owner-token"},
        )

    async def request(self, request: httpx.Request) -> httpx.Response:
        payload = JSON_OBJECT.validate_json(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, payload))
        if request.method == "POST":
            assert request.headers["X-Weave-CSRF"] == "owner-token"
        if path == "/v1/snapshot":
            return httpx.Response(200, json=self.snapshot().model_dump(mode="json"))
        if path == "/v1/events":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.journal)
        if path == "/v1/chat/messages":
            if request.method == "GET":
                return httpx.Response(200, json=self.snapshot().messages.model_dump(mode="json"))
            if self.reject_submit:
                return httpx.Response(503, json={"code": "not_ready"})
            assert payload is not None and isinstance(payload["text"], str)
            message = Message(
                message_id="accepted",
                chat_sequence=len(self.messages) + 1,
                role="user",
                body=payload["text"],
                source_key="client",
            )
            self.messages.append(message)
            return httpx.Response(
                202,
                json=MessageReceipt(
                    message_id=message.message_id, sequence=message.chat_sequence, input_ids=("input",)
                ).model_dump(mode="json"),
            )
        if path == "/v1/executions/job":
            return httpx.Response(
                200,
                json={
                    **self.execution.model_dump(mode="json"),
                    "available_actions": list(self.available_actions),
                    "disabled_reasons": {},
                    "reports": [],
                    "artifacts": [],
                    "children": [],
                    "attentions": [],
                },
            )
        if path in {"/v1/executions/job/pause", "/v1/executions/job/cancel", "/v1/executions/job/resume"}:
            return httpx.Response(
                202, json={**self.execution.model_dump(mode="json"), "available_actions": list(self.available_actions)}
            )
        if path == f"/v1/attentions/{self.attention.attention_id}":
            return httpx.Response(
                200,
                json={
                    **self.attention.model_dump(mode="json"),
                    "execution_objective": self.execution.objective,
                    "space_name": self.space.name,
                    "available_actions": ["resolve"],
                    "disabled_reasons": {},
                },
            )
        if path.endswith("/resolve"):
            return httpx.Response(
                200, json={"message_id": "answer", "attention_revision": self.attention.revision + 1, "execution": None}
            )
        if path == "/v1/spaces/space":
            return httpx.Response(200, json=self.space.model_dump(mode="json"))
        raise AssertionError(f"Unexpected product HTTP request {request.method} {request.url}")

    def writes(self, path: str) -> list[JsonObject]:
        return [
            payload
            for method, target, payload in self.requests
            if method == "POST" and target == path and payload is not None
        ]
