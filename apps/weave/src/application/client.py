import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Literal
from uuid import uuid4

import httpx
from pleiad.contracts import JsonObject
from pydantic import TypeAdapter

from pleiad_weave.infrastructure.storage.product_models import (
    AttentionView,
    ChatMessageRequest,
    Execution,
    Message,
    MessageReceipt,
    MessageView,
    Page,
    ProductEvent,
    ResolutionReceipt,
    ResolutionRequest,
    Snapshot,
)

JSON_OBJECT = TypeAdapter[JsonObject](JsonObject)


class WeaveClient:
    """Connect terminal interfaces to the single running workspace owner."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Bind HTTP ownership without starting another model or product process."""
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport, timeout=30.0)
        self.csrf_token: str | None = None

    async def __aenter__(self) -> "WeaveClient":
        """Enter this interface's network subscription scope."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close subscriptions while product work remains owned by the service."""
        await self.aclose()

    async def aclose(self) -> None:
        """Release this interface's HTTP connections."""
        await self.http.aclose()

    async def get(self, path: str, *, params: dict[str, str | int] | None = None) -> JsonObject:
        """Read a validated product JSON object."""
        response = await self.http.get(path, params=params)
        response.raise_for_status()
        return JSON_OBJECT.validate_json(response.content)

    async def post(self, path: str, payload: JsonObject) -> JsonObject:
        """Submit a host command with the current owner-issued CSRF credential."""
        if self.csrf_token is None:
            await self.snapshot()
        if self.csrf_token is None:
            raise ValueError("The running owner did not issue a CSRF credential")
        response = await self.http.post(path, json=payload, headers={"X-Weave-CSRF": self.csrf_token})
        response.raise_for_status()
        return JSON_OBJECT.validate_json(response.content)

    async def snapshot(self) -> Snapshot:
        """Read one consistent product snapshot and its command credential."""
        value = Snapshot.model_validate(await self.get("/v1/snapshot"))
        token = value.capabilities.get("csrf_token")
        if not isinstance(token, str) or not token:
            raise ValueError("The running owner did not issue a CSRF credential")
        self.csrf_token = token
        return value

    async def messages(self, *, before_sequence: int | None = None, limit: int = 100) -> Page[MessageView]:
        """Read committed main-chat messages without observing SDK messages."""
        params: dict[str, str | int] = {"limit": limit}
        if before_sequence is not None:
            params["before_sequence"] = before_sequence
        return Page[MessageView].model_validate(await self.get("/v1/chat/messages", params=params))

    async def submit(self, request: ChatMessageRequest) -> MessageReceipt:
        """Accept one stable message request in the permanent main chat."""
        payload = JSON_OBJECT.validate_python(request.model_dump(mode="json"))
        return MessageReceipt.model_validate(await self.post("/v1/chat/messages", payload))

    async def resolve(self, attention_id: str, request: ResolutionRequest) -> ResolutionReceipt:
        """Commit one question answer or approval through the owner transaction."""
        payload = JSON_OBJECT.validate_python(request.model_dump(mode="json"))
        return ResolutionReceipt.model_validate(await self.post(f"/v1/attentions/{attention_id}/resolve", payload))

    async def execution(self, execution_id: str) -> JsonObject:
        """Read current work details and server-derived action capabilities."""
        return await self.get(f"/v1/executions/{execution_id}")

    async def attention(self, attention_id: str) -> AttentionView:
        """Read the current persistent question or exact approval action."""
        value = await self.get(f"/v1/attentions/{attention_id}")
        return AttentionView.model_validate(value)

    async def control(
        self,
        execution: Execution,
        action: Literal["pause", "resume", "cancel"],
        *,
        request_id: str | None = None,
    ) -> Execution:
        """Apply a version-checked work control to the inspected record."""
        payload: JsonObject = {
            "request_id": str(uuid4()) if request_id is None else request_id,
            "expected_revision": execution.revision,
        }
        value = await self.post(f"/v1/executions/{execution.execution_id}/{action}", payload)
        return execution_record(value)

    async def events(self, *, after_sequence: int = 0) -> AsyncGenerator[ProductEvent | None, None]:
        """Observe committed product events, yielding None when a snapshot resync is required."""
        async with self.http.stream(
            "GET", "/v1/events", params={"after_sequence": after_sequence}, timeout=None
        ) as response:
            response.raise_for_status()
            name = "message"
            data: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    name = line.removeprefix("event:").strip()
                elif line.startswith("data:"):
                    data.append(line.removeprefix("data:").lstrip())
                elif not line:
                    if name == "resync":
                        yield None
                    elif name == "product" and data:
                        yield ProductEvent.model_validate_json("\n".join(data))
                    name, data = "message", []

    async def wait_for_reply(self, receipt: MessageReceipt, *, after_sequence: int) -> Message:
        """Wait for a formal reply whose saved source batch contains this input."""
        cursor = after_sequence
        while True:
            before: int | None = None
            while True:
                page = await self.messages(before_sequence=before)
                for message in sorted(page.items, key=lambda item: item.chat_sequence):
                    if message.role == "assistant" and receipt.message_id in message.source_message_ids:
                        return message
                if not page.items or min(item.chat_sequence for item in page.items) <= receipt.sequence:
                    break
                if page.next_cursor is None:
                    break
                before = page.next_cursor
            async with aclosing(self.events(after_sequence=cursor)) as events:
                async for event in events:
                    if event is None:
                        cursor = (await self.snapshot()).snapshot_sequence
                        break
                    cursor = event.sequence
                    if event.type.startswith("chat.message"):
                        committed = MessageView.model_validate(event.data)
                        if committed.role == "assistant" and receipt.message_id in committed.source_message_ids:
                            return committed
                else:
                    raise httpx.RemoteProtocolError(
                        "The product event subscription ended before the reply was committed"
                    )


def execution_record(value: JsonObject) -> Execution:
    """Validate the durable work record within its augmented read projection."""
    return Execution.model_validate({key: item for key, item in value.items() if key in Execution.model_fields})


def display_json(value: JsonObject) -> str:
    """Format validated product records for terminal inspection."""
    return json.dumps(value, ensure_ascii=False, indent=2)
