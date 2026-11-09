import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from typer.testing import CliRunner

from pleiad_weave import cli
from pleiad_weave.application.client import WeaveClient
from pleiad_weave.infrastructure.storage.product_models import (
    ApprovalAction,
    Attention,
    Message,
    MessageReceipt,
    MessageView,
    ProductEvent,
)
from tests.http_owner import HttpOwner


def connect_cli(monkeypatch: pytest.MonkeyPatch, owner: HttpOwner) -> None:
    def client(server: str) -> WeaveClient:
        return WeaveClient(server, transport=httpx.MockTransport(owner.request))

    monkeypatch.setattr(cli, "WeaveClient", client)


def test_cli_accepts_one_stable_main_chat_message_without_starting_an_execution_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = HttpOwner()
    connect_cli(monkeypatch, owner)
    result = CliRunner().invoke(
        cli.app, ["run", "Use this report", "--execution", "job", "--message-id", "stable-input", "--no-wait"]
    )
    assert result.exit_code == 0, result.output
    assert '"message_id": "accepted"' in result.output
    requests = owner.writes("/v1/chat/messages")
    assert len(requests) == 1
    assert requests[0]["client_message_id"] == "stable-input"
    assert requests[0]["target_execution_ids"] == ["job"]


def test_cli_wait_returns_only_the_formal_router_reply_attributed_to_its_accepted_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = HttpOwner(
        messages=[
            Message(
                message_id="another",
                chat_sequence=1,
                role="assistant",
                body="Unrelated Worker report",
                source_key="worker",
                source_message_ids=("other-user",),
            )
        ]
    )

    async def request(value: httpx.Request) -> httpx.Response:
        response = await owner.request(value)
        if value.method == "POST" and value.url.path == "/v1/chat/messages":
            owner.messages.append(
                Message(
                    message_id="formal",
                    chat_sequence=3,
                    role="assistant",
                    body="Your request is accepted",
                    source_key="route",
                    source_message_ids=("earlier-user", "accepted"),
                    target_execution_ids=("job",),
                )
            )
        return response

    def client(server: str) -> WeaveClient:
        return WeaveClient(server, transport=httpx.MockTransport(request))

    monkeypatch.setattr(cli, "WeaveClient", client)
    result = CliRunner().invoke(cli.app, ["run", "Research this", "--timeout", "5"])
    assert result.exit_code == 0, result.output
    assert "Your request is accepted" in result.output
    assert "job  running  Research the topic" in result.output
    assert "Unrelated Worker report" not in result.output


def test_cli_approval_and_controls_send_the_current_revision_and_bound_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = HttpOwner(
        attention=Attention(
            attention_id="approval",
            execution_id="job",
            kind="approval",
            action=ApprovalAction(
                tool_code="command", tool_name="command", arguments={"shell_command": "echo checked"}
            ),
            question="Run command?",
            action_digest="exact-action",
            checkpoint_ref="saved",
            revision=6,
        )
    )
    connect_cli(monkeypatch, owner)
    result = CliRunner().invoke(cli.app, ["attention", "approve", "approval"])
    assert result.exit_code == 0, result.output
    resolution = owner.writes("/v1/attentions/approval/resolve")[0]
    assert resolution["decision"] == "approve"
    assert resolution["action_digest"] == "exact-action" and resolution["expected_revision"] == 6
    control = CliRunner().invoke(cli.app, ["execution", "control", "job", "pause"])
    assert control.exit_code == 0, control.output
    assert owner.writes("/v1/executions/job/pause")[0]["expected_revision"] == owner.execution.revision


def test_cli_transmission_failure_reports_the_stable_request_key_for_reviewable_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = HttpOwner(reject_submit=True)
    connect_cli(monkeypatch, owner)
    result = CliRunner().invoke(cli.app, ["run", "Retain the input", "--message-id", "retry-same-key", "--no-wait"])
    assert result.exit_code == 1
    assert "retry-same-key" in result.output
    assert len(owner.writes("/v1/chat/messages")) == 1


def test_native_client_follows_product_events_until_the_reply_contains_its_saved_source_id() -> None:
    async def scenario() -> None:
        owner = HttpOwner()
        unrelated = Message(
            message_id="other",
            chat_sequence=2,
            role="assistant",
            body="Different reply",
            source_key="route-other",
            source_message_ids=("other-input",),
        )
        reply = Message(
            message_id="reply",
            chat_sequence=3,
            role="assistant",
            body="Attributed reply",
            source_key="route",
            source_message_ids=("accepted",),
        )
        for number, value in enumerate((unrelated, reply), start=2):
            event = ProductEvent(
                sequence=number,
                event_id=f"event-{number}",
                subject=value.message_id,
                type="chat.message.created",
                data=MessageView.model_validate(value.model_dump()).model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
            await owner.journal.records.put(f"event: product\ndata: {event.model_dump_json()}\n\n".encode())
        async with WeaveClient(transport=httpx.MockTransport(owner.request)) as client:
            await client.snapshot()
            result = await client.wait_for_reply(
                MessageReceipt(message_id="accepted", sequence=1, input_ids=("input",)), after_sequence=1
            )
            assert result.message_id == "reply" and result.body == "Attributed reply"
        assert owner.journal.closed.is_set()

    asyncio.run(scenario())
