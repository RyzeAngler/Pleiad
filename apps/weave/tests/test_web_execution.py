import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pleiad.contracts import JsonObject
from pleiad.models.providers import openai
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.sandbox.errors import SandboxUnavailableError

from pleiad_weave.agent.contracts import GraphSpec
from pleiad_weave.api.app import create_app
from pleiad_weave.application.dto import ExecuteRequest
from pleiad_weave.application.events import execution_event_from_record
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.application.web import stream_execution_events
from pleiad_weave.infrastructure.bootstrap import create_application
from pleiad_weave.infrastructure.config import Settings
from pleiad_weave.infrastructure.storage.models import ExecutionStatus
from tests.test_execution import PINNED_IMAGE, ScriptedProvider


@pytest.fixture
def web_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[ScriptedProvider], ExecutionService]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "capabilities.yaml").write_text("tools: []\nskills: []\n", encoding="utf-8")

    def create(provider: ScriptedProvider) -> ExecutionService:
        def binding(**options: object) -> ScriptedProvider:
            return provider

        monkeypatch.setattr(openai, "OpenAIProvider", binding)
        return create_application(
            Settings(
                workspace=workspace,
                llm_provider="openai",
                llm_protocol="openai",
                llm_model="test-model",
                llm_api_key="test-key",
                llm_vision_model=None,
                llm_speech_model=None,
                log_console=False,
                sandbox_image=PINNED_IMAGE,
            )
        )

    return create


def graph_responses(graph_id: str, value: str) -> tuple[str | JsonObject, ...]:
    graph: JsonObject = {
        "graph_id": graph_id,
        "nodes": [{"node_id": "write", "objective": "Write a result"}],
        "outputs": [{"source_node_id": "write", "source_path": "/text", "target_path": "/text"}],
    }
    return (
        {"name": "submit_graph", "arguments": {"graph": graph}},
        {"name": "execute_graph", "arguments": {"graph_id": graph_id, "input": {"topic": value}}},
        json.dumps({"text": value}),
        f"Finished {value}.",
    )


def test_web_history_filters_executions_by_exact_conversation(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service(ScriptedProvider(("first answer", "second answer")))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            first = await service.submit(ExecuteRequest(objective="first", conversation_id="one"))
            await service.wait(first.execution_id)
            second = await service.submit(ExecuteRequest(objective="second", conversation_id="two"))
            await service.wait(second.execution_id)
            response = await client.get("/v1/executions", params={"conversation_id": "one"})
            assert response.status_code == 200
            assert [item["execution_id"] for item in response.json()] == [first.execution_id]
            assert response.json()[0]["output"] == {"text": "first answer"}
            assert (await client.get("/v1/executions", params={"conversation_id": "missing"})).json() == []

    asyncio.run(scenario())


def test_web_execution_preserves_unreachable_sandbox_error_after_command_fails(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(({"name": "command", "arguments": {"command": "python --version"}},))
        service = web_service(provider)
        commands: list[tuple[str, ...]] = []

        async def unavailable_process(
            command: tuple[str, ...],
            *,
            stdin: bytes | None,
            output_limit: int,
            timeout_seconds: float | None,
        ) -> ProcessResult:
            commands.append(command)
            return ProcessResult(1, b"", b"Cannot connect to the Docker daemon", False, 0.0, False)

        service.weave.sandbox.backend.process_executor = unavailable_process
        service.weave.sandbox.backend.requires_runtime_binary = False
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            submitted = await client.post("/v1/executions", json={"objective": "Check Python in the sandbox"})
            assert submitted.status_code == 202
            execution_id = submitted.json()["execution"]["execution_id"]
            with pytest.raises(SandboxUnavailableError):
                await service.wait(execution_id)

            response = await client.get(f"/v1/executions/{execution_id}")
            assert response.status_code == 200
            execution = response.json()["execution"]
            assert execution["status"] == "failed"
            assert execution["error"]["code"] == "sandbox.runtime.unavailable"
            assert "docker info" in execution["error"]["message"]
            assert "cleanup" not in execution["error"]["message"].lower()
            assert commands == [("docker", "info")]
            assert len(provider.requests) == 1

    asyncio.run(scenario())


def test_web_details_expose_real_node_input_and_owned_artifact_without_other_executions(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service(ScriptedProvider((*graph_responses("first", "one"), *graph_responses("second", "two"))))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            first = await service.submit(ExecuteRequest(objective="first graph"))
            await service.wait(first.execution_id)
            second = await service.submit(ExecuteRequest(objective="second graph"))
            await service.wait(second.execution_id)
            response = await client.get(f"/v1/executions/{first.execution_id}/details")
            assert response.status_code == 200
            payload = response.json()
            assert [item["graph_id"] for item in payload["graphs"]] == ["first"]
            assert [item["execution_id"] for item in payload["graph_runs"]] == [first.execution_id]
            assert [item["input"] for item in payload["node_runs"]] == [{"topic": "one"}]
            assert [item["model_name"] for item in payload["node_runs"]] == ["test-model"]
            assert payload["artifacts"][0]["node_run_id"] == payload["node_runs"][0]["node_run_id"]
            assert payload["artifacts"][0]["value"] == {"text": "one"}
            assert "location" not in payload["artifacts"][0]
            assert (await client.get("/v1/executions/missing/details")).status_code == 404

    asyncio.run(scenario())


def test_repeated_graph_execution_keeps_node_attempts_and_skips_in_their_own_run_after_restart(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "repeated",
            "nodes": [
                {
                    "node_id": "first",
                    "objective": "Produce a value",
                    "output_schema": {"type": "object", "required": ["value"]},
                },
                {"node_id": "second", "objective": "Use the value"},
            ],
            "edges": [{"edge_id": "next", "source_node_id": "first", "target_node_id": "second"}],
        }
        execute: JsonObject = {"name": "execute_graph", "arguments": {"graph_id": "repeated"}}
        provider = ScriptedProvider(
            (
                {"name": "submit_graph", "arguments": {"graph": graph}},
                execute,
                '{"value": 1}',
                '{"text": "first run output"}',
                execute,
                "{}",
                "The second run failed.",
            )
        )
        service = web_service(provider)
        app = create_app(service)
        async with app.router.lifespan_context(app):
            queued = await service.submit(ExecuteRequest(objective="Run the graph twice"))
            await service.wait(queued.execution_id)

        restarted = web_service(ScriptedProvider(()))
        app = create_app(restarted)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.get(f"/v1/executions/{queued.execution_id}/details")
            assert response.status_code == 200
            payload = response.json()
            first, second = payload["graph_runs"]
            assert first["status"] == "succeeded" and second["status"] == "failed"
            assert first["graph_run_id"] != second["graph_run_id"]
            first_nodes = [item for item in payload["node_runs"] if item["graph_run_id"] == first["graph_run_id"]]
            second_nodes = [item for item in payload["node_runs"] if item["graph_run_id"] == second["graph_run_id"]]
            assert [(item["node_id"], item["status"]) for item in first_nodes] == [
                ("first", "succeeded"),
                ("second", "succeeded"),
            ]
            assert [(item["node_id"], item["status"]) for item in second_nodes] == [("first", "failed")]
            assert {item["node_id"]: item["status"] for item in second["state"]["node_states"]} == {
                "first": "failed",
                "second": "skipped",
            }
            old_output = next(item for item in first_nodes if item["node_id"] == "second")
            assert next(
                item["value"] for item in payload["artifacts"] if item["node_run_id"] == old_output["node_run_id"]
            ) == {"text": "first run output"}

    asyncio.run(scenario())


def test_web_details_preserve_a_skipped_conditional_branch_without_fabricating_a_node_attempt(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "conditional",
            "nodes": [
                {"node_id": "decide", "objective": "Choose whether to continue"},
                {"node_id": "optional", "objective": "Run only when requested"},
            ],
            "edges": [
                {
                    "edge_id": "continue",
                    "source_node_id": "decide",
                    "target_node_id": "optional",
                    "condition": {"source_path": "/continue", "operator": "truthy"},
                }
            ],
        }
        service = web_service(
            ScriptedProvider(
                (
                    {"name": "submit_graph", "arguments": {"graph": graph}},
                    {"name": "execute_graph", "arguments": {"graph_id": "conditional"}},
                    '{"continue": false}',
                    "The optional step was skipped.",
                )
            )
        )
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            queued = await service.submit(ExecuteRequest(objective="Run conditional steps"))
            await service.wait(queued.execution_id)
            response = await client.get(f"/v1/executions/{queued.execution_id}/details")
            assert response.status_code == 200
            payload = response.json()
            run = payload["graph_runs"][0]
            assert run["status"] == "succeeded"
            assert {item["node_id"]: item["status"] for item in run["state"]["node_states"]} == {
                "decide": "succeeded",
                "optional": "skipped",
            }
            assert run["state"]["branch_decisions"] == [{"edge_id": "continue", "activated": False}]
            assert [item["node_id"] for item in payload["node_runs"]] == ["decide"]
            assert payload["node_runs"][0]["graph_run_id"] == run["graph_run_id"]

    asyncio.run(scenario())


def test_web_approval_canvas_keeps_the_frozen_graph_before_any_nodes_run(
    web_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["weave.execute_graph"]')

    async def scenario() -> None:
        provider = ScriptedProvider(
            ({"name": "execute_graph", "arguments": {"graph_id": "saved-plan"}}, "An unrelated answer.")
        )
        service = web_service(provider)
        original = GraphSpec.model_validate(
            {"graph_id": "saved-plan", "nodes": [{"node_id": "write", "objective": "Approved original"}]}
        )
        service.weave.graph_store.submit(original)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            queued = await service.submit(ExecuteRequest(objective="Run the saved plan", conversation_id="approval"))
            waiting = await service.wait(queued.execution_id)
            assert waiting is not None and waiting.status is ExecutionStatus.WAITING_APPROVAL
            service.weave.graph_store.submit(original.model_copy(update={"revision": 2}))
            response = await client.get(f"/v1/executions/{queued.execution_id}/details")
            assert response.status_code == 200
            payload = response.json()
            assert payload["graphs"] == [original.model_dump(mode="json")]
            assert payload["execution"]["graph_revision"] == 1
            assert payload["graph_runs"] == [] and payload["node_runs"] == [] and payload["artifacts"] == []
            unrelated = await service.submit(ExecuteRequest(objective="Another task", conversation_id="other"))
            await service.wait(unrelated.execution_id)
            other = await client.get(f"/v1/executions/{unrelated.execution_id}/details")
            assert other.status_code == 200 and other.json()["graphs"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("escape", ["identity", "symlink"])
def test_web_details_refuse_artifact_references_outside_the_artifact_store(
    web_service: Callable[[ScriptedProvider], ExecutionService], escape: str
) -> None:
    async def scenario() -> None:
        service = web_service(ScriptedProvider(graph_responses("example", "one")))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            queued = await service.submit(ExecuteRequest(objective="create artifact"))
            await service.wait(queued.execution_id)
            node = service.weave.graph_executor.nodes.list_records()[0]
            if escape == "identity":
                service.weave.graph_executor.nodes.save(node.model_copy(update={"artifact_id": "../outside"}))
            else:
                outside = service.weave.workspace / "outside.json"
                outside.write_text('{"text":"must not leak"}', encoding="utf-8")
                path = service.weave.graph_executor.artifacts.root / f"{node.artifact_id}.json"
                path.unlink()
                path.symlink_to(outside)
            response = await client.get(f"/v1/executions/{queued.execution_id}/details")
            assert response.status_code == 500
            assert response.json() == {"detail": "The recorded node artifact cannot be read."}

    asyncio.run(scenario())


def test_web_messages_restore_original_message_order_and_tool_result_pairing(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            ({"name": "read_file", "arguments": {"path": "note.txt", "scope": "workspace"}}, "A real answer.")
        )
        service = web_service(provider)
        (service.weave.workspace / "note.txt").write_text("A note.", encoding="utf-8")
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            queued = await service.submit(ExecuteRequest(objective="Read my note", conversation_id="chat"))
            await service.wait(queued.execution_id)
            response = await client.get("/v1/conversations/chat/messages")
            assert response.status_code == 200
            messages = response.json()
            assert [item["role"] for item in messages] == ["user", "assistant", "tool", "assistant"]
            assert messages[0]["content"] == "Read my note"
            assert messages[-1]["content"] == "A real answer."
            assert messages[2]["tool_call_id"] == messages[1]["tool_calls"][0]["id"]
            assert messages[1]["turn_id"] == messages[2]["turn_id"]
            assert len({item["message_id"] for item in messages}) == 4
            assert [item["turn_sequence"] for item in messages] == [1, 1, 1, 2]
            missing = await client.get("/v1/conversations/missing/messages")
            assert missing.status_code == 404
            assert missing.json() == {"detail": "conversation not found"}
            assert "missing" not in service.weave.memory.memories

    asyncio.run(scenario())


def test_new_space_conversation_has_an_empty_message_history_before_execution(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service(ScriptedProvider(()))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            space = await client.post("/v1/spaces", json={"name": "Research"})
            conversation = await client.post(
                f"/v1/spaces/{space.json()['space_id']}/conversations", json={"title": "First task"}
            )
            response = await client.get(f"/v1/conversations/{conversation.json()['conversation_id']}/messages")
            assert response.status_code == 200
            assert response.json() == []
            assert service.repository.list_records() == ()

    asyncio.run(scenario())


def test_event_stream_delivers_live_text_and_each_durable_event_once(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("Live answer.",))
        provider.block = True
        service = web_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="stream"))
            await provider.started.wait()
            stream = stream_execution_events(service, queued.execution_id)
            frames = [await anext(stream)]
            provider.release.set()
            async with asyncio.timeout(5):
                frames.extend([frame async for frame in stream])
            payloads = [json.loads(frame.split("data: ")[1]) for frame in frames]
            durable = [item["sequence"] for item in payloads if item["sequence"] > 0]
            replay = await service.events(queued.execution_id)
            assert durable == [event.sequence for event in replay]
            assert len(durable) == len(set(durable))
            deltas = [item for item in payloads if item["event_type"] == "message.text.delta"]
            assert [item["data"]["delta"] for item in deltas] == ["Live answer."]
            assert all(item["sequence"] == 0 for item in deltas)
            assert payloads[-1]["event_type"] == "execution.completed"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_disconnecting_a_browser_stream_does_not_cancel_its_execution(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("Still finished.",))
        provider.block = True
        service = web_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="keep running"))
            await provider.started.wait()
            stream = stream_execution_events(service, queued.execution_id)
            await anext(stream)
            await stream.aclose()
            provider.release.set()
            record = await service.wait(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.SUCCEEDED
            assert record.output == {"text": "Still finished."}
            assert not provider.cancelled
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_live_journal_notifications_arriving_out_of_order_preserve_all_committed_events(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service(ScriptedProvider(()))
        try:
            queued = await service.create_execution(ExecuteRequest(objective="observe ordered commits"))
            stream = stream_execution_events(service, queued.execution_id)
            await anext(stream)
            first = service.repository.append_event(queued.execution_id, "progress.first", {})
            second = service.repository.append_event(queued.execution_id, "progress.second", {})
            service.event_bus.publish(execution_event_from_record(second))
            service.event_bus.publish(execution_event_from_record(first))
            async with asyncio.timeout(5):
                frames = [await anext(stream), await anext(stream)]
            payloads = [json.loads(frame.split("data: ")[1]) for frame in frames]
            assert [item["sequence"] for item in payloads] == [first.sequence, second.sequence]
            assert [item["event_type"] for item in payloads] == ["progress.first", "progress.second"]
            await stream.aclose()
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_event_stream_overflow_explicitly_requests_resynchronization(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service(ScriptedProvider(()))
        try:
            queued = await service.create_execution(ExecuteRequest(objective="observe only"))
            stream = stream_execution_events(service, queued.execution_id, queue_limit=1)
            await anext(stream)
            await service.weave.publisher.emit(queued.execution_id, "progress.one")
            await service.weave.publisher.emit(queued.execution_id, "progress.two")
            barrier = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(barrier.set_result, None)
            await barrier
            assert await anext(stream) == 'event: resync\ndata: {"reason":"buffer_overflow"}\n\n'
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_http_event_stream_resumes_after_last_event_id_and_rejects_invalid_cursors(
    web_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service(ScriptedProvider(("Finished.",)))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            queued = await service.submit(ExecuteRequest(objective="replay"))
            await service.wait(queued.execution_id)
            response = await client.get(f"/v1/executions/{queued.execution_id}/stream", headers={"Last-Event-ID": "2"})
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            payloads = [json.loads(frame.split("data: ")[1]) for frame in response.text.strip().split("\n\n")]
            assert all(item["sequence"] > 2 for item in payloads)
            assert payloads[-1]["event_type"] == "execution.completed"
            invalid = await client.get(f"/v1/executions/{queued.execution_id}/stream", headers={"Last-Event-ID": "bad"})
            assert invalid.status_code == 400
            assert (await client.get("/v1/executions/missing/stream")).status_code == 404

    asyncio.run(scenario())
