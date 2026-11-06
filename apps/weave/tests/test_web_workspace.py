import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from threading import Event as ThreadEvent

import pytest
from httpx import ASGITransport, AsyncClient
from pleiad.contracts import JsonObject
from pleiad.messages.message import UserMessage
from pleiad.models.providers import openai
from starlette.types import Message, Scope

from pleiad_weave.agent.contracts import GraphSpec
from pleiad_weave.agent.weave import GraphControls
from pleiad_weave.api.app import create_app
from pleiad_weave.application.dto import ExecuteRequest
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.bootstrap import create_application
from pleiad_weave.infrastructure.config import Settings
from pleiad_weave.infrastructure.storage.memory import MemoryFact
from pleiad_weave.infrastructure.storage.models import (
    ExecutionEventInput,
    ExecutionEventRecord,
    ExecutionRecord,
    ExecutionStatus,
)
from pleiad_weave.infrastructure.storage.repository import ExecutionRepository
from pleiad_weave.infrastructure.storage.workspaces import WorkspaceConflictError
from tests.test_execution import PINNED_IMAGE, ScriptedProvider


@pytest.mark.parametrize("target", ["conversation", "space"])
def test_delete_removes_owned_history_after_restart_without_touching_another_space(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService], target: str
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "deleted-trip",
            "nodes": [{"node_id": "plan", "objective": "Plan a trip"}],
            "outputs": [{"source_node_id": "plan", "source_path": "/value", "target_path": "/value"}],
        }
        service = web_service_factory(
            ScriptedProvider(
                (
                    {"name": "submit_graph", "arguments": {"graph": graph}},
                    {"name": "execute_graph", "arguments": {"graph_id": "deleted-trip", "input": {}}},
                    '{"value":"Private itinerary"}',
                    "Trip ready",
                    "Keep this answer",
                )
            )
        )
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            space = (await client.post("/v1/spaces", json={"name": "Delete travel"})).json()
            kept_space = (await client.post("/v1/spaces", json={"name": "Keep writing"})).json()
            conversation = (
                await client.post(f"/v1/spaces/{space['space_id']}/conversations", json={"title": "Delete trip"})
            ).json()
            kept = (
                await client.post(f"/v1/spaces/{kept_space['space_id']}/conversations", json={"title": "Keep article"})
            ).json()
            conversation_id = conversation["conversation_id"]
            queued = await service.submit(ExecuteRequest(objective="Plan trip", conversation_id=conversation_id))
            completed = await service.wait(queued.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            details = (await client.get(f"/v1/executions/{queued.execution_id}/details")).json()
            assert details["artifacts"][0]["value"] == {"value": "Private itinerary"}
            artifact_paths = tuple(service.weave.graph_executor.artifacts.root.glob("*.json"))
            assert artifact_paths
            kept_execution = await service.submit(
                ExecuteRequest(objective="Keep article", conversation_id=kept["conversation_id"])
            )
            await service.wait(kept_execution.execution_id)
            service.weave.memory.save_fact(
                MemoryFact(
                    memory_id="private-fact",
                    scope="conversation",
                    scope_id=conversation_id,
                    content="Private preference",
                )
            )
            service.weave.memory.save_fact(
                MemoryFact(
                    memory_id="kept-fact",
                    scope="conversation",
                    scope_id=kept["conversation_id"],
                    content="Keep preference",
                )
            )
            path = f"/v1/spaces/{space['space_id']}" if target == "space" else f"/v1/conversations/{conversation_id}"
            response = await client.delete(path)
            assert response.status_code == 200 and response.json() == {"deleted": True}
            assert (await client.delete(path)).status_code == 404
            assert not any(path.exists() for path in artifact_paths)
            assert service.weave.manifest.read_text() == "tools: []\nskills: []\n"

        restarted = web_service_factory(ScriptedProvider(()))
        app = create_app(restarted)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            assert restarted.repository.get(queued.execution_id) is None
            assert restarted.repository.read_events(queued.execution_id) == ()
            assert restarted.repository.resume_state(queued.execution_id) is None
            assert restarted.weave.graph_executor.graphs.list_records() == ()
            assert restarted.weave.graph_executor.nodes.list_records() == ()
            assert restarted.weave.graph_store.revision("deleted-trip", 1) is None
            assert await restarted.weave.memory.session(conversation_id).read() == ()
            assert restarted.weave.memory.list_facts("conversation", conversation_id=conversation_id) == ()
            assert (
                restarted.weave.memory.list_facts("conversation", conversation_id=kept["conversation_id"])[0].content
                == "Keep preference"
            )
            assert (await client.get(f"/v1/conversations/{conversation_id}/messages")).status_code == 404
            assert (await client.get(f"/v1/executions/{queued.execution_id}/details")).status_code == 404
            assert (await client.get(f"/v1/executions/{queued.execution_id}/events")).status_code == 404
            assert (
                await client.post(
                    "/v1/executions", json={"objective": "Stale tab submit", "conversation_id": conversation_id}
                )
            ).status_code == 404
            assert restarted.repository.get(kept_execution.execution_id) is not None
            messages = (await client.get(f"/v1/conversations/{kept['conversation_id']}/messages")).json()
            assert any(message["content"] == "Keep this answer" for message in messages)
            spaces = (await client.get("/v1/spaces")).json()
            assert {item["space_id"] for item in spaces} == (
                {kept_space["space_id"]} if target == "space" else {space["space_id"], kept_space["space_id"]}
            )

    asyncio.run(scenario())


def test_delete_rejects_running_tasks_without_changing_their_history(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("Finished normally",))
        provider.block = True
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            space = (await client.post("/v1/spaces", json={"name": "Running"})).json()
            conversation = (
                await client.post(f"/v1/spaces/{space['space_id']}/conversations", json={"title": "Running task"})
            ).json()
            queued = await service.submit(
                ExecuteRequest(objective="Wait for provider", conversation_id=conversation["conversation_id"])
            )
            await provider.started.wait()
            for path in (f"/v1/spaces/{space['space_id']}", f"/v1/conversations/{conversation['conversation_id']}"):
                assert (await client.delete(path)).status_code == 409
            assert service.workspaces.get_conversation(conversation["conversation_id"]) is not None
            provider.release.set()
            completed = await service.wait(queued.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            messages = (await client.get(f"/v1/conversations/{conversation['conversation_id']}/messages")).json()
            assert any(message["content"] == "Finished normally" for message in messages)

    asyncio.run(scenario())


def test_delete_preserves_a_saved_graph_and_other_tasks_using_its_revision(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "shared-trip",
            "nodes": [{"node_id": "plan", "objective": "Plan reusable trip"}],
            "outputs": [{"source_node_id": "plan", "source_path": "/value", "target_path": "/value"}],
        }
        service = web_service_factory(
            ScriptedProvider(
                (
                    {"name": "submit_graph", "arguments": {"graph": graph}},
                    "Graph ready",
                    {"name": "execute_graph", "arguments": {"graph_id": "shared-trip", "input": {}}},
                    '{"value":"Retained itinerary"}',
                    "Done",
                )
            )
        )
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            space = (await client.post("/v1/spaces", json={"name": "Travel"})).json()
            conversation = (
                await client.post(f"/v1/spaces/{space['space_id']}/conversations", json={"title": "Original"})
            ).json()
            queued = await service.submit(
                ExecuteRequest(objective="Build reusable trip", conversation_id=conversation["conversation_id"])
            )
            await service.wait(queued.execution_id)
            settings = {"name": "Travel", "saved_graph": {"graph_id": "shared-trip", "revision": 1}}
            assert (
                await client.patch(f"/v1/spaces/{space['space_id']}", json={**settings, "expected_revision": 1})
            ).status_code == 200
            assert (await client.delete(f"/v1/conversations/{conversation['conversation_id']}")).status_code == 200
            assert (await client.get(f"/v1/spaces/{space['space_id']}/saved-graph")).status_code == 200
            assert (
                await client.patch(
                    f"/v1/spaces/{space['space_id']}", json={**settings, "name": "Renamed", "expected_revision": 2}
                )
            ).status_code == 200
            other = (await client.post("/v1/spaces", json={"name": "Another space"})).json()
            kept = (
                await client.post(f"/v1/spaces/{other['space_id']}/conversations", json={"title": "Reuse graph"})
            ).json()
            reused = await service.submit(
                ExecuteRequest(objective="Reuse trip", conversation_id=kept["conversation_id"])
            )
            completed = await service.wait(reused.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            assert (await client.delete(f"/v1/spaces/{space['space_id']}")).status_code == 200
            details = (await client.get(f"/v1/executions/{reused.execution_id}/details")).json()
            assert details["graphs"][0]["graph_id"] == "shared-trip"
            assert details["artifacts"][0]["value"] == {"value": "Retained itinerary"}
            assert (await client.delete(f"/v1/spaces/{other['space_id']}")).status_code == 200
            assert service.weave.graph_store.revision("shared-trip", 1) is None

    asyncio.run(scenario())


def test_delete_during_graph_resolution_preserves_history_until_the_other_task_finishes(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "resolving-trip",
            "nodes": [{"node_id": "plan", "objective": "Plan trip"}],
        }
        service = web_service_factory(
            ScriptedProvider(
                (
                    {"name": "submit_graph", "arguments": {"graph": graph}},
                    "Original graph ready",
                    {"name": "execute_graph", "arguments": {"graph_id": "resolving-trip", "input": {}}},
                    "{}",
                    "Reuse finished",
                )
            )
        )
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            space = (await client.post("/v1/spaces", json={"name": "Travel"})).json()
            original = (
                await client.post(f"/v1/spaces/{space['space_id']}/conversations", json={"title": "Original"})
            ).json()
            reused = (
                await client.post(f"/v1/spaces/{space['space_id']}/conversations", json={"title": "Reuse"})
            ).json()
            empty = (await client.post(f"/v1/spaces/{space['space_id']}/conversations", json={"title": "Empty"})).json()
            source = await service.submit(
                ExecuteRequest(objective="Build trip", conversation_id=original["conversation_id"])
            )
            await service.wait(source.execution_id)
            resolved = asyncio.Event()
            release = asyncio.Event()
            refresh = GraphControls.refresh

            async def pause_after_resolution(controls: GraphControls) -> None:
                if (
                    controls.execution_id != source.execution_id
                    and controls.execution_id in service.weave.execution_graphs
                ):
                    resolved.set()
                    await release.wait()
                await refresh(controls)

            monkeypatch.setattr(GraphControls, "refresh", pause_after_resolution)
            running = await service.submit(
                ExecuteRequest(objective="Reuse trip", conversation_id=reused["conversation_id"])
            )
            await resolved.wait()
            try:
                assert service.weave.graph_executor.graphs.list_records() == ()
                pending = service.repository.get(running.execution_id)
                assert pending is not None and pending.graph_id is None
                assert (await client.delete(f"/v1/conversations/{original['conversation_id']}")).status_code == 409
                messages = (await client.get(f"/v1/conversations/{original['conversation_id']}/messages")).json()
                assert any(message["content"] == "Original graph ready" for message in messages)
                assert (await client.delete(f"/v1/conversations/{empty['conversation_id']}")).status_code == 200
            finally:
                release.set()
            completed = await service.wait(running.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            assert (await client.delete(f"/v1/conversations/{original['conversation_id']}")).status_code == 200
            assert service.weave.graph_store.revision("resolving-trip", 1) is not None

    asyncio.run(scenario())


def test_repeated_submit_cancellation_cannot_create_an_execution_after_task_deletion(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("Completed",))
        provider.block = True
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            space = (await client.post("/v1/spaces", json={"name": "Travel"})).json()
            conversation = (
                await client.post(f"/v1/spaces/{space['space_id']}/conversations", json={"title": "Pending commit"})
            ).json()
            conversation_id = conversation["conversation_id"]
            loop = asyncio.get_running_loop()
            entered = asyncio.Event()
            release = ThreadEvent()
            create = ExecutionRepository.create

            def pause_commit(
                repository: ExecutionRepository, record: ExecutionRecord, *, event: ExecutionEventInput
            ) -> tuple[ExecutionRecord, ExecutionEventRecord]:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(timeout=10)
                return create(repository, record, event=event)

            monkeypatch.setattr(ExecutionRepository, "create", pause_commit)
            submitted = asyncio.create_task(
                service.submit(ExecuteRequest(objective="Finish committing", conversation_id=conversation_id))
            )
            await entered.wait()
            try:
                for cancellation in (1, 2):
                    submitted.cancel()
                    assert submitted.cancelling() == cancellation
                    delivered = asyncio.Event()
                    loop.call_soon(delivered.set)
                    await delivered.wait()
                assert not submitted.done()
                assert service.workspace_mutation_lock.locked()
                deleted = asyncio.create_task(service.delete_workspace(conversation_id=conversation_id))
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await submitted
            with pytest.raises(WorkspaceConflictError):
                await deleted
            await provider.started.wait()
            records = service.repository.list_records(conversation_id)
            assert len(records) == 1 and records[0].status is ExecutionStatus.RUNNING
            assert service.workspaces.get_conversation(conversation_id) is not None
            provider.release.set()
            completed = await service.wait(records[0].execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED

    asyncio.run(scenario())


def test_delete_parent_task_preserves_a_child_execution_owned_by_another_task(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service_factory(ScriptedProvider(("Parent completed",)))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            space = (await client.post("/v1/spaces", json={"name": "Related tasks"})).json()
            parent = (
                await client.post(f"/v1/spaces/{space['space_id']}/conversations", json={"title": "Parent"})
            ).json()
            child = (await client.post(f"/v1/spaces/{space['space_id']}/conversations", json={"title": "Child"})).json()
            queued = await service.submit(
                ExecuteRequest(objective="Parent work", conversation_id=parent["conversation_id"])
            )
            await service.wait(queued.execution_id)
            service.repository.create(
                ExecutionRecord(
                    execution_id="child-execution",
                    parent_execution_id=queued.execution_id,
                    conversation_id=child["conversation_id"],
                    objective="Child work",
                    status=ExecutionStatus.SUCCEEDED,
                ),
                event=ExecutionEventInput(event_type="execution.completed"),
            )
            assert (await client.delete(f"/v1/conversations/{parent['conversation_id']}")).status_code == 409
            assert service.repository.get(queued.execution_id) is not None
            assert service.repository.get("child-execution") is not None
            messages = (await client.get(f"/v1/conversations/{parent['conversation_id']}/messages")).json()
            assert any(message["content"] == "Parent completed" for message in messages)
            assert (await client.delete(f"/v1/conversations/{child['conversation_id']}")).status_code == 200
            assert (await client.delete(f"/v1/conversations/{parent['conversation_id']}")).status_code == 200

    asyncio.run(scenario())


@pytest.fixture
def web_service_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[ScriptedProvider], ExecutionService]:
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
                capability_file=workspace / "capabilities.yaml",
                llm_provider="openai",
                llm_protocol="openai",
                llm_model="workspace-test-model",
                llm_api_key="secret-not-for-resource-api",
                llm_base_url="https://private-provider.invalid/v1",
                llm_vision_model=None,
                llm_speech_model=None,
                log_console=False,
                sandbox_image=PINNED_IMAGE,
            )
        )

    return create


def test_space_conversations_remain_scoped_after_service_restart(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        first = web_service_factory(ScriptedProvider(()))
        app = create_app(first)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.post("/v1/spaces", json={"name": "Travel", "instructions": "Prefer quiet places"})
            assert response.status_code == 201
            travel = response.json()
            response = await client.post("/v1/spaces", json={"name": "Writing"})
            writing = response.json()
            response = await client.post(f"/v1/spaces/{travel['space_id']}/conversations", json={"title": "Kyoto"})
            assert response.status_code == 201
            conversation = response.json()
        second = web_service_factory(ScriptedProvider(()))
        restarted = create_app(second)
        async with (
            restarted.router.lifespan_context(restarted),
            AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as client,
        ):
            response = await client.get("/v1/spaces")
            assert response.json() == [travel, writing]
            response = await client.get(f"/v1/spaces/{travel['space_id']}/conversations")
            assert response.json() == [conversation]
            response = await client.get(f"/v1/spaces/{writing['space_id']}/conversations")
            assert response.json() == []

    asyncio.run(scenario())


def test_stale_space_update_does_not_overwrite_newer_rules(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service_factory(ScriptedProvider(()))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            created = await client.post("/v1/spaces", json={"name": "Travel"})
            identity = created.json()["space_id"]
            update = {"name": "Travel", "instructions": "New rules", "expected_revision": 1}
            response = await client.patch(f"/v1/spaces/{identity}", json=update)
            assert response.status_code == 200
            assert response.json()["revision"] == 2
            response = await client.patch(f"/v1/spaces/{identity}", json={**update, "instructions": "Stale rules"})
            assert response.status_code == 409
            response = await client.get("/v1/spaces")
            assert response.json()[0]["instructions"] == "New rules"

    asyncio.run(scenario())


def test_execution_keeps_server_space_snapshot_and_original_user_message(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("First answer", "Second answer"))
        provider.block = True
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            created = await client.post(
                "/v1/spaces",
                json={"name": "Travel", "instructions": "First rules", "preferred_tool_codes": ["read_file"]},
            )
            identity = created.json()["space_id"]
            conversation = await client.post(f"/v1/spaces/{identity}/conversations", json={"title": "Weekend trip"})
            conversation_id = conversation.json()["conversation_id"]
            response = await client.post(
                "/v1/executions",
                json={
                    "objective": "Plan a quiet weekend",
                    "conversation_id": conversation_id,
                    "input": {"budget": 200, "weave_space": {"instructions": "Client spoof"}},
                },
            )
            first_id = response.json()["execution"]["execution_id"]
            await provider.started.wait()
            await client.patch(
                f"/v1/spaces/{identity}",
                json={"name": "Travel", "instructions": "Second rules", "expected_revision": 1},
            )
            response = await client.post(
                "/v1/executions", json={"objective": "Plan another weekend", "conversation_id": conversation_id}
            )
            second_id = response.json()["execution"]["execution_id"]
            provider.release.set()
            first = await service.wait(first_id)
            second = await service.wait(second_id)
            assert first is not None and second is not None
            assert first.status is ExecutionStatus.SUCCEEDED and second.status is ExecutionStatus.SUCCEEDED
            assert first.objective == "Plan a quiet weekend" and first.input["budget"] == 200
            first_context = first.input["weave_space"]
            second_context = second.input["weave_space"]
            assert isinstance(first_context, dict) and isinstance(second_context, dict)
            assert first_context["instructions"] == "First rules" and first_context["revision"] == 1
            assert second_context["instructions"] == "Second rules" and second_context["revision"] == 2
            assert "First rules" in provider.requests[0].model_dump_json()
            assert "Client spoof" not in provider.requests[0].model_dump_json()
            turns = await service.weave.memory.session(conversation_id).read()
            user_messages = [
                message.content for turn in turns for message in turn.messages if isinstance(message, UserMessage)
            ]
            assert user_messages == ["Plan a quiet weekend", "Plan another weekend"]

    asyncio.run(scenario())


@pytest.mark.parametrize("instructions", ["", "   "])
def test_cleared_default_instructions_are_not_sent_with_the_next_task(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService], instructions: str
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("Done",))
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            created = await client.post("/v1/spaces", json={"name": "Travel", "instructions": "Old preference"})
            space_id = created.json()["space_id"]
            updated = await client.patch(
                f"/v1/spaces/{space_id}",
                json={"name": "Travel", "instructions": instructions, "expected_revision": 1},
            )
            assert updated.status_code == 200
            conversation = await client.post(f"/v1/spaces/{space_id}/conversations", json={"title": "New task"})
            queued = await service.submit(
                ExecuteRequest(objective="Plan this trip", conversation_id=conversation.json()["conversation_id"])
            )
            completed = await service.wait(queued.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            context = completed.input["weave_space"]
            assert isinstance(context, dict) and "instructions" not in context
            user = next(message for message in provider.requests[0].messages if message.role == "user")
            assert '"instructions"' not in str(user.content)
            assert "Old preference" not in str(user.content)

    asyncio.run(scenario())


def test_every_graph_node_receives_execution_defaults_without_model_input_copying(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        empty_schema: JsonObject = {"type": "object", "additionalProperties": False}
        graph: JsonObject = {
            "graph_id": "default-instructions",
            "input_schema": empty_schema,
            "nodes": [
                {"node_id": "research", "objective": "Research routes", "input_schema": empty_schema},
                {"node_id": "plan", "objective": "Plan the route", "input_schema": empty_schema},
            ],
            "edges": [{"edge_id": "research-plan", "source_node_id": "research", "target_node_id": "plan"}],
        }
        provider = ScriptedProvider(
            (
                {"name": "submit_graph", "arguments": {"graph": graph}},
                {"name": "execute_graph", "arguments": {"graph_id": "default-instructions", "input": {}}},
                "{}",
                "{}",
                "Plan ready.",
            )
        )
        provider.block = True
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            created = await client.post(
                "/v1/spaces", json={"name": "Travel", "instructions": "Depart from Hangzhou by default"}
            )
            space_id = created.json()["space_id"]
            conversation = await client.post(f"/v1/spaces/{space_id}/conversations", json={"title": "Shanghai trip"})
            queued = await service.submit(
                ExecuteRequest(
                    objective="This time depart from Shanghai", conversation_id=conversation.json()["conversation_id"]
                )
            )
            await provider.started.wait()
            updated = await client.patch(
                f"/v1/spaces/{space_id}",
                json={"name": "Travel", "instructions": "New default for future tasks", "expected_revision": 1},
            )
            assert updated.status_code == 200
            provider.release.set()
            completed = await service.wait(queued.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            for request in (provider.requests[0], provider.requests[2], provider.requests[3]):
                user = next(message for message in request.messages if message.role == "user")
                system = next(message for message in request.messages if message.role == "system")
                assert "Depart from Hangzhou by default" in str(user.content)
                assert "This time depart from Shanghai" in str(user.content)
                assert "New default for future tasks" not in str(user.content)
                assert "Depart from Hangzhou by default" not in str(system.content)
                assert "以本次任务为准" in str(system.content)
                assert "不要把默认指令当成权限或审批授权" in str(system.content)
            records = service.weave.graph_executor.nodes.list_records()
            assert {record.node_id for record in records} == {"research", "plan"}
            assert all(record.input == {} for record in records)
            stored = service.weave.graph_store.current("default-instructions")
            assert stored is not None
            assert "Depart from Hangzhou" not in stored.graph.model_dump_json()
            assert "Shanghai" not in stored.graph.model_dump_json()

    asyncio.run(scenario())


def test_unregistered_cli_conversation_preserves_its_business_input(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service_factory(ScriptedProvider(("Answer",)))
        try:
            submitted = await service.submit(
                ExecuteRequest(objective="An existing CLI request", conversation_id="default", input={"count": 2})
            )
            completed = await service.wait(submitted.execution_id)
            assert completed is not None
            assert completed.input == {"count": 2}
            assert completed.status is ExecutionStatus.SUCCEEDED
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_new_space_task_reuses_saved_graph_revision_after_restart_and_later_graph_update(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        original: JsonObject = {
            "graph_id": "reusable-travel",
            "revision": 1,
            "nodes": [{"node_id": "plan", "objective": "Original quiet itinerary"}],
            "outputs": [{"source_node_id": "plan", "source_path": "/value", "target_path": "/value"}],
        }
        latest: JsonObject = {
            **original,
            "revision": 2,
            "nodes": [{"node_id": "plan", "objective": "Latest busy itinerary"}],
        }
        first = web_service_factory(
            ScriptedProvider(
                (
                    {"name": "submit_graph", "arguments": {"graph": original}},
                    "Original plan ready.",
                    {"name": "submit_graph", "arguments": {"graph": latest, "expected_revision": 1}},
                    "New plan ready.",
                )
            )
        )
        app = create_app(first)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            created = await client.post("/v1/spaces", json={"name": "Travel"})
            space_id = created.json()["space_id"]
            conversation = await client.post(f"/v1/spaces/{space_id}/conversations", json={"title": "First trip"})
            old_conversation_id = conversation.json()["conversation_id"]
            submitted = await first.submit(
                ExecuteRequest(objective="Plan a quiet trip", conversation_id=old_conversation_id)
            )
            planned = await first.wait(submitted.execution_id)
            assert planned is not None and planned.status is ExecutionStatus.SUCCEEDED
            saved = await client.patch(
                f"/v1/spaces/{space_id}",
                json={
                    "name": "Travel",
                    "instructions": "Current reusable task preference",
                    "saved_graph": {"graph_id": "reusable-travel", "revision": 1},
                    "expected_revision": 1,
                },
            )
            assert saved.status_code == 200
            submitted = await first.submit(
                ExecuteRequest(objective="Try a busier plan", conversation_id=old_conversation_id)
            )
            updated = await first.wait(submitted.execution_id)
            assert updated is not None and updated.graph_revision == 2

        provider = ScriptedProvider(
            (
                {"name": "execute_graph", "arguments": {"graph_id": "reusable-travel", "input": {"city": "Kyoto"}}},
                '{"value":"quiet Kyoto itinerary"}',
                "Saved plan completed.",
            )
        )
        second = web_service_factory(provider)
        restarted = create_app(second)
        async with (
            restarted.router.lifespan_context(restarted),
            AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as client,
        ):
            before_preview = second.repository.list_records()
            preview = await client.get(f"/v1/spaces/{space_id}/saved-graph")
            assert preview.status_code == 200
            assert preview.json() == GraphSpec.model_validate(original).model_dump(mode="json")
            assert second.repository.list_records() == before_preview
            assert provider.requests == []
            conversation = await client.post(f"/v1/spaces/{space_id}/conversations", json={"title": "Another trip"})
            new_conversation_id = conversation.json()["conversation_id"]
            assert new_conversation_id != old_conversation_id
            submitted = await second.submit(
                ExecuteRequest(objective="Use the saved plan for Kyoto", conversation_id=new_conversation_id)
            )
            completed = await second.wait(submitted.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            assert (completed.graph_id, completed.graph_revision) == ("reusable-travel", 1)
            context = completed.input["weave_space"]
            assert isinstance(context, dict)
            assert context["saved_graph"] == {"graph_id": "reusable-travel", "revision": 1}
            assert "inspect_graph" in str(context["saved_graph_usage"])
            assert "execute_graph" in provider.requests[0].model_dump_json()
            assert "Original quiet itinerary" in provider.requests[1].model_dump_json()
            assert "Latest busy itinerary" not in provider.requests[1].model_dump_json()
            node_user = next(message for message in provider.requests[1].messages if message.role == "user")
            assert "Current reusable task preference" in str(node_user.content)
            assert "Use the saved plan for Kyoto" in str(node_user.content)
            original_revision = second.weave.graph_store.revision("reusable-travel", 1)
            assert original_revision is not None
            assert "Current reusable task preference" not in original_revision.graph.model_dump_json()
            turns = await second.weave.memory.session(new_conversation_id).read()
            assert [
                message.content for turn in turns for message in turn.messages if isinstance(message, UserMessage)
            ] == ["Use the saved plan for Kyoto"]

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["space", "saved_graph"])
def test_saved_graph_preview_returns_not_found_without_starting_a_task(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService], missing: str
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(())
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            space_id = "f" * 32
            if missing == "saved_graph":
                created = await client.post("/v1/spaces", json={"name": "Travel"})
                assert created.status_code == 201
                space_id = created.json()["space_id"]
            response = await client.get(f"/v1/spaces/{space_id}/saved-graph")
            assert response.status_code == 404
            assert response.json() == {
                "detail": "Space not found" if missing == "space" else "No Graph saved in this space"
            }
            assert service.repository.list_records() == ()
            assert provider.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_reference", ["missing_graph", "missing_revision", "other_space"])
def test_space_rejects_saved_graph_without_existing_revision_from_its_own_task(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService], invalid_reference: str
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "owned-plan",
            "nodes": [{"node_id": "write", "objective": "Write a post"}],
        }
        service = web_service_factory(
            ScriptedProvider(({"name": "submit_graph", "arguments": {"graph": graph}}, "Plan ready."))
        )
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            created = await client.post("/v1/spaces", json={"name": "Writing"})
            space_id = created.json()["space_id"]
            conversation = await client.post(f"/v1/spaces/{space_id}/conversations", json={"title": "First post"})
            submitted = await service.submit(
                ExecuteRequest(objective="Plan a post", conversation_id=conversation.json()["conversation_id"])
            )
            planned = await service.wait(submitted.execution_id)
            assert planned is not None and planned.status is ExecutionStatus.SUCCEEDED
            if invalid_reference == "other_space":
                other = await client.post("/v1/spaces", json={"name": "Travel"})
                space_id = other.json()["space_id"]
            reference = {
                "graph_id": "missing-plan" if invalid_reference == "missing_graph" else "owned-plan",
                "revision": 2 if invalid_reference == "missing_revision" else 1,
            }
            rejected = await client.patch(
                f"/v1/spaces/{space_id}", json={"name": "Workspace", "saved_graph": reference, "expected_revision": 1}
            )
            assert rejected.status_code == 422
            current = service.workspaces.get_space(space_id)
            assert current is not None and current.revision == 1 and current.saved_graph is None

    asyncio.run(scenario())


def test_clearing_saved_graph_removes_it_from_future_task_context(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "clearable-plan",
            "nodes": [{"node_id": "write", "objective": "Write a post"}],
        }
        service = web_service_factory(
            ScriptedProvider(({"name": "submit_graph", "arguments": {"graph": graph}}, "Plan ready.", "Fresh task."))
        )
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            created = await client.post("/v1/spaces", json={"name": "Writing"})
            space_id = created.json()["space_id"]
            conversation = await client.post(f"/v1/spaces/{space_id}/conversations", json={"title": "First post"})
            submitted = await service.submit(
                ExecuteRequest(objective="Plan a post", conversation_id=conversation.json()["conversation_id"])
            )
            planned = await service.wait(submitted.execution_id)
            assert planned is not None and planned.status is ExecutionStatus.SUCCEEDED
            saved = await client.patch(
                f"/v1/spaces/{space_id}",
                json={
                    "name": "Writing",
                    "saved_graph": {"graph_id": "clearable-plan", "revision": 1},
                    "expected_revision": 1,
                },
            )
            assert saved.status_code == 200
            cleared = await client.patch(
                f"/v1/spaces/{space_id}", json={"name": "Writing", "saved_graph": None, "expected_revision": 2}
            )
            assert cleared.status_code == 200 and cleared.json()["saved_graph"] is None
            conversation = await client.post(f"/v1/spaces/{space_id}/conversations", json={"title": "New task"})
            submitted = await service.submit(
                ExecuteRequest(objective="Start from scratch", conversation_id=conversation.json()["conversation_id"])
            )
            completed = await service.wait(submitted.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            context = completed.input["weave_space"]
            assert isinstance(context, dict) and context["saved_graph"] is None
            assert "saved_graph_usage" not in context
            assert completed.graph_id is None and completed.graph_revision is None
            assert service.weave.graph_store.revision("clearable-plan", 1) is not None

    asyncio.run(scenario())


def test_resource_catalog_exposes_live_metadata_without_provider_secrets(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service_factory(ScriptedProvider(()))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            skill = service.weave.workspace / "skills" / "writing"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: writing\ndescription: Write concise prose\n---\n\nPrivate writing instructions.\n",
                encoding="utf-8",
            )
            service.weave.manifest.write_text(
                "tools: []\nskills:\n  - name: writing\n    code: writing\n    description: Write concise prose\n",
                encoding="utf-8",
            )
            response = await client.get("/v1/resources")
            assert response.status_code == 200
            payload = response.json()
            assert payload["models"] == [{"role": "text", "model": "workspace-test-model"}]
            assert {tool["code"] for tool in payload["tools"]} == {"read_file", "write_file", "command"}
            assert payload["skills"] == [{"code": "writing", "name": "writing", "description": "Write concise prose"}]
            assert "secret-not-for-resource-api" not in response.text
            assert "private-provider.invalid" not in response.text
            assert "Private writing instructions" not in response.text

    asyncio.run(scenario())


@pytest.mark.parametrize("resource", ["spaces", "spaces/missing/conversations"])
def test_workspace_names_cannot_be_blank(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService], resource: str
) -> None:
    async def scenario() -> None:
        service = web_service_factory(ScriptedProvider(()))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            key = "name" if resource == "spaces" else "title"
            response = await client.post(f"/v1/{resource}", json={key: "   "})
            assert response.status_code == 422

    asyncio.run(scenario())


def test_space_draft_without_default_instructions_can_be_created(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(('{"reply":"Ready","draft":{"name":"Travel","instructions":""}}',))
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            proposed = await client.post(
                "/v1/spaces/draft", json={"messages": [{"role": "user", "content": "Create a travel space"}]}
            )
            assert proposed.status_code == 200
            assert proposed.json()["draft"]["instructions"] == ""
            created = await client.post("/v1/spaces", json=proposed.json()["draft"])
            assert created.status_code == 201 and created.json()["instructions"] == ""

    asyncio.run(scenario())


def test_space_draft_can_be_refined_without_creating_a_space_or_task(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        initial = {
            "name": "Travel",
            "instructions": "Quiet trips",
            "preferred_tool_codes": [],
            "preferred_skill_codes": [],
        }
        revised = {
            **initial,
            "name": "My travel space",
            "instructions": "Quiet trips, cite sources",
            "preferred_tool_codes": ["read_file"],
        }
        provider = ScriptedProvider(
            (
                json.dumps({"reply": "Review these settings.", "draft": initial}),
                json.dumps({"reply": "Added the source rule.", "draft": revised}),
            )
        )
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            messages = [{"role": "user", "content": "Create a quiet travel planning space"}]
            proposal = await client.post("/v1/spaces/draft", json={"messages": messages})
            assert proposal.status_code == 200
            assert proposal.json() == {"reply": "Review these settings.", "draft": initial}
            messages.extend(
                [
                    {"role": "assistant", "content": proposal.json()["reply"]},
                    {"role": "user", "content": "Cite sources too"},
                ]
            )
            edited = {**initial, "name": "My travel space"}
            refinement = await client.post("/v1/spaces/draft", json={"messages": messages, "draft": edited})
            assert refinement.status_code == 200
            assert refinement.json()["draft"] == revised
            content = provider.requests[1].messages[-1].content
            assert isinstance(content, str)
            supplied = json.loads(content)
            assert supplied["messages"] == messages
            assert supplied["draft"] == edited
            assert (await client.get("/v1/spaces")).json() == []
            assert service.repository.list_records() == ()
            created = await client.post("/v1/spaces", json=refinement.json()["draft"])
            assert created.status_code == 201
            assert created.json()["instructions"] == "Quiet trips, cite sources"
            assert created.json()["preferred_tool_codes"] == ["read_file"]
            assert created.json()["saved_graph"] is None
            assert (await client.get(f"/v1/spaces/{created.json()['space_id']}/conversations")).json() == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "output",
    [
        "not JSON",
        '{"reply":"ok","draft":{"name":"Travel","preferred_tool_codes":["invented"]}}',
        '{"reply":" ","draft":{"name":"Travel"}}',
    ],
)
def test_invalid_space_draft_never_becomes_a_saved_configuration(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService], output: str
) -> None:
    async def scenario() -> None:
        service = web_service_factory(ScriptedProvider((output,)))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.post(
                "/v1/spaces/draft", json={"messages": [{"role": "user", "content": "Travel planning"}]}
            )
            assert response.status_code == 502
            assert response.json()["detail"]
            assert (await client.get("/v1/spaces")).json() == []

    asyncio.run(scenario())


def test_space_draft_provider_failure_has_a_safe_error_and_no_saved_space(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(())
        provider.fail = True
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.post(
                "/v1/spaces/draft", json={"messages": [{"role": "user", "content": "Travel planning"}]}
            )
            assert response.status_code == 502
            assert "secret-not-for-resource-api" not in response.text
            assert "private-provider.invalid" not in response.text
            assert "model.provider_unavailable" not in response.text
            assert (await client.get("/v1/spaces")).json() == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "configuration",
    [
        {"name": "Travel", "saved_graph": None},
        {"name": "Travel", "preferred_skill_codes": ["not-installed"]},
        {"name": "Travel", "preferred_tool_codes": ["not-installed"]},
    ],
)
def test_create_space_rejects_removed_fields_and_unconfigured_resource_preferences(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService], configuration: JsonObject
) -> None:
    async def scenario() -> None:
        service = web_service_factory(ScriptedProvider(()))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.post("/v1/spaces", json=configuration)
            assert response.status_code == 422
            assert (await client.get("/v1/spaces")).json() == []

    asyncio.run(scenario())


def test_drafting_cannot_write_files_even_when_the_model_requests_a_tool(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            ({"name": "write_file", "arguments": {"path": "forbidden.txt", "content": "unexpected"}},)
        )
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.post(
                "/v1/spaces/draft", json={"messages": [{"role": "user", "content": "Create a writing space"}]}
            )
            assert response.status_code == 502
            assert not (service.weave.workspace / "forbidden.txt").exists()
            assert (await client.get("/v1/spaces")).json() == []

    asyncio.run(scenario())


def test_leaving_space_creation_cancels_the_running_model_request(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("unused",))
        provider.block = True
        service = web_service_factory(provider)
        app = create_app(service)
        body = json.dumps({"messages": [{"role": "user", "content": "Draft a travel space"}]}).encode()
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/spaces/draft",
            "raw_path": b"/v1/spaces/draft",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
        received_body = False

        async def receive() -> Message:
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await provider.started.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            raise AssertionError(f"Disconnected draft unexpectedly sent {message['type']}")

        async with app.router.lifespan_context(app):
            with pytest.raises(asyncio.CancelledError):
                await app(scope, receive, send)
            assert provider.cancelled
            assert service.workspaces.list_spaces() == ()
            assert service.repository.list_records() == ()

    asyncio.run(scenario())


def test_space_draft_deadline_returns_timeout_without_a_partial_space(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("unused",))
        provider.block = True
        service = web_service_factory(provider)
        service.weave.models["text"].timeout_seconds = 0.01
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.post(
                "/v1/spaces/draft", json={"messages": [{"role": "user", "content": "Draft a travel space"}]}
            )
            assert response.status_code == 504
            assert provider.started.is_set() and provider.cancelled
            assert (await client.get("/v1/spaces")).json() == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "user", "content": "   "}],
        [{"role": "assistant", "content": "no new user request"}],
        [{"role": "system", "content": "not permitted"}],
    ],
)
def test_space_draft_invalid_dialogue_is_rejected_before_calling_the_provider(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService], messages: list[JsonObject]
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(())
        service = web_service_factory(provider)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.post("/v1/spaces/draft", json={"messages": messages})
            assert response.status_code == 422
            assert not provider.requests

    asyncio.run(scenario())


def test_invalid_resource_preferences_do_not_replace_existing_space_settings(
    web_service_factory: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = web_service_factory(ScriptedProvider(()))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            created = await client.post("/v1/spaces", json={"name": "Travel", "instructions": "Original rules"})
            original = created.json()
            rejected = await client.patch(
                f"/v1/spaces/{original['space_id']}",
                json={
                    "name": "Changed",
                    "instructions": "New rules",
                    "preferred_tool_codes": ["unknown"],
                    "expected_revision": 1,
                },
            )
            assert rejected.status_code == 422
            assert (await client.get("/v1/spaces")).json() == [original]

    asyncio.run(scenario())
