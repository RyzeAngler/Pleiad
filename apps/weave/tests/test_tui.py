import asyncio

import httpx
from textual.widgets import Button, Input, ListView

from pleiad_weave.application.client import WeaveClient
from pleiad_weave.infrastructure.storage.product_models import ApprovalAction, Attention, ExecutionStatus, Message
from pleiad_weave.tui.app import PleiadApp
from tests.http_owner import HttpOwner


def test_tui_sends_running_work_input_through_the_single_main_chat_and_detaches() -> None:
    async def scenario() -> None:
        owner = HttpOwner()
        client = WeaveClient(transport=httpx.MockTransport(owner.request))
        app = PleiadApp(client)
        async with app.run_test(size=(130, 42)) as pilot:
            await asyncio.wait_for(owner.journal.started.wait(), timeout=3)
            listing = app.query_one("#entities", ListView)
            listing.index = 0
            listing.action_select_cursor()
            await pilot.pause()
            await pilot.click("#target")
            composer = app.query_one("#composer-input", Input)
            composer.value = "Use the newly available report"
            composer.focus()
            await pilot.press("enter")
            assert owner.writes("/v1/chat/messages")[0]["target_execution_ids"] == ["job"]
            assert composer.value == ""
            assert app.state.messages["accepted"].body == "Use the newly available report"
            assert len(app.query(Input)) == 1
            assert owner.execution.status is ExecutionStatus.RUNNING
        assert client.http.is_closed
        assert owner.journal.closed.is_set()
        assert not owner.writes("/v1/executions/job/cancel")

    asyncio.run(scenario())


def test_tui_attention_answer_is_one_atomic_resolution_without_a_duplicate_chat_submit() -> None:
    async def scenario() -> None:
        owner = HttpOwner()
        app = PleiadApp(WeaveClient(transport=httpx.MockTransport(owner.request)))
        async with app.run_test(size=(130, 42)) as pilot:
            await asyncio.wait_for(owner.journal.started.wait(), timeout=3)
            await pilot.click("#attentions")
            listing = app.query_one("#entities", ListView)
            listing.index = 0
            listing.action_select_cursor()
            await pilot.pause()
            await pilot.click("#answer")
            composer = app.query_one("#composer-input", Input)
            composer.value = "Use the primary source"
            await pilot.press("enter")
            writes = owner.writes("/v1/attentions/question/resolve")
            assert len(writes) == 1
            assert writes[0]["answer"] == "Use the primary source"
            assert writes[0]["expected_revision"] == owner.attention.revision
            assert not owner.writes("/v1/chat/messages")
            assert composer.value == "" and app.state.answer_to is None

    asyncio.run(scenario())


def test_tui_approval_requires_a_button_bound_to_the_current_action_digest() -> None:
    async def scenario() -> None:
        owner = HttpOwner(
            attention=Attention(
                attention_id="approval",
                execution_id="job",
                kind="approval",
                action=ApprovalAction(
                    tool_code="command", tool_name="command", arguments={"shell_command": "echo checked"}
                ),
                question="Execute this script?",
                checkpoint_ref="checkpoint",
                action_digest="digest",
                revision=4,
            )
        )
        app = PleiadApp(WeaveClient(transport=httpx.MockTransport(owner.request)))
        async with app.run_test(size=(130, 42)) as pilot:
            await asyncio.wait_for(owner.journal.started.wait(), timeout=3)
            await pilot.click("#attentions")
            listing = app.query_one("#entities", ListView)
            listing.index = 0
            listing.action_select_cursor()
            await pilot.pause()
            composer = app.query_one("#composer-input", Input)
            composer.value = "approve"
            composer.focus()
            await pilot.press("enter")
            assert len(owner.writes("/v1/chat/messages")) == 1
            assert not owner.writes("/v1/attentions/approval/resolve")
            await pilot.click("#approve")
            decision = owner.writes("/v1/attentions/approval/resolve")[0]
            assert decision["decision"] == "approve"
            assert decision["expected_revision"] == 4 and decision["action_digest"] == "digest"

    asyncio.run(scenario())


def test_tui_failed_message_preserves_draft_and_retry_identity_after_a_fresh_read() -> None:
    async def scenario() -> None:
        owner = HttpOwner(reject_submit=True)
        app = PleiadApp(WeaveClient(transport=httpx.MockTransport(owner.request)))
        async with app.run_test(size=(130, 42)) as pilot:
            await asyncio.wait_for(owner.journal.started.wait(), timeout=3)
            composer = app.query_one("#composer-input", Input)
            composer.value = "Retain this draft"
            composer.focus()
            await pilot.press("enter")
            assert composer.value == "Retain this draft" and not composer.disabled
            assert not app.state.messages
            owner.reject_submit = False
            await app.action_reconnect()
            composer.focus()
            await pilot.press("enter")
            writes = owner.writes("/v1/chat/messages")
            assert len(writes) == 2
            assert writes[0]["client_message_id"] == writes[1]["client_message_id"]
            assert composer.value == ""

    asyncio.run(scenario())


def test_tui_resync_preserves_draft_and_reloads_selected_work_outside_the_active_snapshot() -> None:
    async def scenario() -> None:
        owner = HttpOwner(
            messages=[
                Message(
                    message_id="first", chat_sequence=1, role="assistant", body="Committed reply", source_key="router"
                )
            ]
        )
        app = PleiadApp(WeaveClient(transport=httpx.MockTransport(owner.request)))
        async with app.run_test(size=(130, 42)) as pilot:
            await asyncio.wait_for(owner.journal.started.wait(), timeout=3)
            listing = app.query_one("#entities", ListView)
            listing.index = 0
            listing.action_select_cursor()
            await pilot.pause()
            composer = app.query_one("#composer-input", Input)
            composer.value = "Still editing"
            owner.execution = owner.execution.model_copy(update={"status": ExecutionStatus.PAUSED, "revision": 3})
            owner.available_actions = ("resume", "cancel")
            owner.sequence += 1
            await owner.journal.records.put(b"event: resync\ndata: {}\n\n")
            await pilot.pause()
            assert composer.value == "Still editing"
            assert app.state.selected_id == "job"
            assert app.state.execution is not None and app.state.execution.revision == 3
            assert not app.query_one("#resume", Button).disabled
            await pilot.click("#resume")
            assert owner.writes("/v1/executions/job/resume")[0]["expected_revision"] == 3

    asyncio.run(scenario())


def test_tui_late_detail_response_cannot_replace_the_current_navigation_or_enable_old_work_controls() -> None:
    async def scenario() -> None:
        owner = HttpOwner()
        requested, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        block = False

        async def request(value: httpx.Request) -> httpx.Response:
            response = await owner.request(value)
            if block and value.url.path == "/v1/executions/job":
                requested.set()
                await release.wait()
                returned.set()
            return response

        app = PleiadApp(WeaveClient(transport=httpx.MockTransport(request)))
        async with app.run_test(size=(130, 42)) as pilot:
            await asyncio.wait_for(owner.journal.started.wait(), 3)
            listing = app.query_one("#entities", ListView)
            listing.index = 0
            listing.action_select_cursor()
            await pilot.pause()
            initial_execution = app.state.execution
            assert initial_execution is not None
            block = True
            await owner.journal.records.put(b"event: resync\ndata: {}\n\n")
            await asyncio.wait_for(requested.wait(), 3)
            await pilot.click("#spaces")
            listing.index = 0
            listing.action_select_cursor()
            await pilot.pause()
            assert app.state.category == "spaces" and app.state.selected_id == "space"
            release.set()
            await asyncio.wait_for(returned.wait(), 3)
            await pilot.pause()
            assert app.state.execution is None and app.state.attention is None
            assert app.state.detail is not None and app.state.detail["space_id"] == "space"
            assert app.query_one("#cancel", Button).disabled and app.query_one("#target", Button).disabled
            assert not owner.writes("/v1/executions/job/cancel")

    asyncio.run(scenario())


def test_tui_discards_an_older_snapshot_that_returns_after_a_newer_message_refresh() -> None:
    async def scenario() -> None:
        owner = HttpOwner()
        stale_started, release_stale, fresh_read, stale_returned = tuple(asyncio.Event() for number in range(4))
        snapshot_requests = 0

        async def request(value: httpx.Request) -> httpx.Response:
            nonlocal snapshot_requests
            if value.url.path == "/v1/snapshot":
                snapshot_requests += 1
                if snapshot_requests == 2:
                    stale = owner.snapshot().model_dump(mode="json")
                    stale_started.set()
                    await release_stale.wait()
                    stale_returned.set()
                    return httpx.Response(200, json=stale)
                if snapshot_requests == 3:
                    owner.execution = owner.execution.model_copy(
                        update={"status": ExecutionStatus.PAUSED, "revision": 2}
                    )
                    owner.available_actions = ("resume", "cancel")
            response = await owner.request(value)
            if value.method == "POST" and value.url.path == "/v1/chat/messages":
                await owner.journal.records.put(b"event: resync\ndata: {}\n\n")
                await asyncio.wait_for(stale_started.wait(), 3)
            if snapshot_requests >= 3 and value.url.path == "/v1/executions/job":
                fresh_read.set()
            return response

        app = PleiadApp(WeaveClient(transport=httpx.MockTransport(request)))
        async with app.run_test(size=(130, 42)) as pilot:
            await asyncio.wait_for(owner.journal.started.wait(), 3)
            listing = app.query_one("#entities", ListView)
            listing.index = 0
            listing.action_select_cursor()
            await pilot.pause()
            composer = app.query_one("#composer-input", Input)
            composer.value = "Refresh after acceptance"
            composer.focus()
            await pilot.press("enter")
            await asyncio.wait_for(fresh_read.wait(), 3)
            generation = app.state.cache_generation
            release_stale.set()
            await asyncio.wait_for(stale_returned.wait(), 3)
            await pilot.pause()
            assert app.state.cache_generation == generation
            assert app.state.snapshot is not None
            assert app.state.snapshot.executions.items[0].revision == 2
            assert app.state.snapshot.executions.items[0].status is ExecutionStatus.PAUSED
            assert app.state.execution is not None and app.state.execution.revision == 2
            assert not app.query_one("#resume", Button).disabled
            assert len(owner.writes("/v1/chat/messages")) == 1

    asyncio.run(scenario())


def test_tui_offline_composer_remains_editable_and_enter_keeps_the_unsent_draft() -> None:
    async def scenario() -> None:
        owner = HttpOwner()
        offline = False

        async def request(value: httpx.Request) -> httpx.Response:
            if offline and value.url.path == "/v1/snapshot":
                return httpx.Response(503, json={"code": "owner_unavailable"})
            return await owner.request(value)

        app = PleiadApp(WeaveClient(transport=httpx.MockTransport(request)))
        async with app.run_test(size=(130, 42)) as pilot:
            await asyncio.wait_for(owner.journal.started.wait(), 3)
            composer = app.query_one("#composer-input", Input)
            composer.value = "Keep this draft"
            offline = True
            await app.action_reconnect()
            initially_connected = app.state.connected
            assert not initially_connected and not composer.disabled
            composer.focus()
            await pilot.press("end", "!", "enter")
            assert composer.value == "Keep this draft!"
            assert not owner.writes("/v1/chat/messages")
            offline = False
            await app.action_reconnect()
            assert app.state.connected and composer.value == "Keep this draft!"

    asyncio.run(scenario())
