import asyncio
import fcntl
import json
import logging
import re
import shutil
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Literal

import pytest
from fastmcp import FastMCP
from fastmcp.client.transports import FastMCPTransport, StreamableHttpTransport
from httpx import ASGITransport, AsyncClient, HTTPStatusError, Request, Response
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pleiad.contracts import JsonObject
from pleiad.errors.base import SystemFault
from pleiad.mcp import stdio as stdio_module
from pleiad.messages.message import ToolMessage, UserMessage
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import (
    OpenAIResponse,
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    OpenAIStreamDelta,
    OpenAIStreamFunction,
    OpenAIStreamToolCall,
    OpenAIUsage,
)
from pleiad.models.providers import openai
from pleiad.observability.tracing import Observability
from pleiad.orchestration.graph.node import Node
from pleiad.orchestration.graph.state import GraphStatus, NodeStatus
from pleiad.runtime.lifecycle.state import RunStatus
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.tools.base import ToolFailure
from pleiad.tools.contracts import ToolContext
from pleiad.tools.errors import ToolError
from typer.testing import CliRunner

from pleiad_weave import cli
from pleiad_weave.agent.contracts import GraphSpec
from pleiad_weave.agent.nodes import NodeExecutor
from pleiad_weave.api.app import create_app
from pleiad_weave.application import service as service_module
from pleiad_weave.application.dto import ExecuteRequest
from pleiad_weave.application.errors import ApprovalConflictError
from pleiad_weave.application.events import ExecutionEvent
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.capabilities import mcp as mcp_module
from pleiad_weave.capabilities.mcp import (
    McpConfiguration,
    McpConnection,
    WorkspaceOAuth,
    create_mcp_server,
    load_mcp_configuration,
)
from pleiad_weave.infrastructure.bootstrap import create_application
from pleiad_weave.infrastructure.config import Settings
from pleiad_weave.infrastructure.logging import LoggingConfig, configure_logging, shutdown_logging
from pleiad_weave.infrastructure.storage.graphs import TaskGraphStore
from pleiad_weave.infrastructure.storage.memory import MemoryFact, MemoryStore
from pleiad_weave.infrastructure.storage.models import (
    ApprovalDecision,
    ExecutionEventInput,
    ExecutionRecord,
    ExecutionStatus,
    GraphRunRecord,
    NodeRunRecord,
)
from pleiad_weave.tui.app import PleiadApp
from pleiad_weave.tui.projection import TuiProjection
from pleiad_weave.tui.state import AssistantMessageView, TuiState

PINNED_IMAGE = "python:3.12-slim@sha256:" + "a" * 64


class ScriptedProvider:
    def __init__(self, responses: tuple[str | JsonObject, ...], *, report_usage: bool = True) -> None:
        self.responses = iter(responses)
        self.report_usage = report_usage
        self.requests: list[OpenAIRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.fail = False
        self.closed = False
        self.cancelled = False

    async def complete(self, request: OpenAIRequest) -> OpenAIResponse:
        raise AssertionError("Weave should consume the shared SDK streaming execution path")

    async def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        self.requests.append(request)
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
            if self.fail:
                raise SystemFault("model.provider_unavailable", "Provider unavailable")
            response = next(self.responses)
            if isinstance(response, dict):
                call = OpenAIStreamToolCall(
                    index=0,
                    id=f"call-{len(self.requests)}",
                    function=OpenAIStreamFunction(
                        name=str(response["name"]), arguments=json.dumps(response["arguments"])
                    ),
                )
                delta = OpenAIStreamDelta(tool_calls=(call,))
                finish = "tool_calls"
            else:
                delta = OpenAIStreamDelta(content=response)
                finish = "stop"
            yield OpenAIStreamChunk(model="test-model", choices=(OpenAIStreamChoice(delta=delta),))
            yield OpenAIStreamChunk(
                model="test-model",
                choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(), finish_reason=finish),),
                usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2) if self.report_usage else None,
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self) -> None:
        self.closed = True


class UsageOverridesProvider(ScriptedProvider):
    def __init__(self, responses: tuple[str | JsonObject, ...], usage_overrides: dict[int, int | None]) -> None:
        super().__init__(responses)
        self.usage_overrides = usage_overrides

    async def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        async for chunk in super().stream(request):
            number = len(self.requests)
            if number in self.usage_overrides and chunk.usage is not None:
                count = self.usage_overrides[number]
                usage = (
                    None
                    if count is None
                    else OpenAIUsage(
                        prompt_tokens=1,
                        completion_tokens=count,
                        total_tokens=count + 1,
                    )
                )
                chunk = chunk.model_copy(update={"usage": usage})
            yield chunk


@pytest.fixture
def make_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[ScriptedProvider], ExecutionService]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "capabilities.yaml").write_text("tools: []\nskills: []\n", encoding="utf-8")
    monkeypatch.setenv("PLEIAD_MCP_CONFIG_FILE", str(tmp_path / "mcp.json"))

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
                llm_model="test-model",
                llm_api_key="test-key",
                llm_vision_model=None,
                llm_speech_model=None,
                log_console=False,
                sandbox_image=PINNED_IMAGE,
            )
        )

    return create


def test_execution_uses_sdk_file_tool_and_journals_paired_call_identities(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            (
                {"name": "read_file", "arguments": {"path": "note.txt", "scope": "workspace"}},
                "The note says hello.",
            )
        )
        service = make_service(provider)
        (service.weave.workspace / "note.txt").write_text("hello", encoding="utf-8")
        try:
            queued = await service.submit(ExecuteRequest(objective="Read the note"))
            record = await service.wait(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.SUCCEEDED
            assert record.output == {"text": "The note says hello."}
            events = await service.events(queued.execution_id)
            assert [event.sequence for event in events] == list(range(1, len(events) + 1))
            calls = [event for event in events if event.event_type == "tool.started"]
            results = [event for event in events if event.event_type == "tool.completed"]
            assert calls[0].data["call_id"] == results[0].data["call_id"]
            assert calls[0].data["run_id"] == results[0].data["run_id"]
            assert calls[0].data["run_id"] != queued.execution_id
            assert results[0].data["ok"] is True
            assert "hello" in json.dumps(provider.requests[1].model_dump())
            assert all("message" not in event.data for event in events)
            assert all(event.event_type != "message.text.delta" for event in events)
        finally:
            await service.shutdown()
        assert provider.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_explicit_approval_defers_real_file_action_and_retries_decision_without_reexecution(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
    decision: Literal["approve", "reject"],
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        provider = ScriptedProvider(
            (
                {"name": "write_file", "arguments": {"path": "approved.txt", "content": "approved contents"}},
                "finished",
            )
        )
        service = make_service(provider)
        target = service.weave.workspace / "approved.txt"
        try:
            queued = await service.submit(ExecuteRequest(objective="Write the note"))
            paused = await service.wait(queued.execution_id)
            assert paused is not None and paused.status is ExecutionStatus.WAITING_APPROVAL
            assert not target.exists()
            pending = await service.approvals(queued.execution_id)
            assert len(pending) == 1
            assert pending[0].interruption.arguments == {"path": "approved.txt", "content": "approved contents"}
            interruption_id = pending[0].interruption.interruption_id
            await service.decide(queued.execution_id, interruption_id, decision)
            completed = await service.wait(queued.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            if decision == "approve":
                assert target.read_text() == "approved contents"
            else:
                assert not target.exists()
                assert "weave.approval_denied" in provider.requests[1].model_dump_json()
            replay = await service.decide(queued.execution_id, interruption_id, decision)
            assert replay == completed
            with pytest.raises(ApprovalConflictError, match="different decision"):
                await service.decide(
                    queued.execution_id, interruption_id, "reject" if decision == "approve" else "approve"
                )
            assert len(provider.requests) == 2
            assert await service.approvals(queued.execution_id) == ()
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_partial_approval_preserves_completed_action_before_resolving_the_remaining_call(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    class MultiToolProvider(ScriptedProvider):
        async def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
            if self.requests:
                async for chunk in super().stream(request):
                    yield chunk
                return
            self.requests.append(request)
            yield OpenAIStreamChunk(
                model="test-model",
                choices=(
                    OpenAIStreamChoice(
                        delta=OpenAIStreamDelta(
                            tool_calls=(
                                OpenAIStreamToolCall(
                                    index=0,
                                    id="first-action",
                                    function=OpenAIStreamFunction(
                                        name="write_file",
                                        arguments='{"path":"first.txt","content":"one"}',
                                    ),
                                ),
                                OpenAIStreamToolCall(
                                    index=1,
                                    id="second-action",
                                    function=OpenAIStreamFunction(
                                        name="write_file",
                                        arguments='{"path":"second.txt","content":"two"}',
                                    ),
                                ),
                            )
                        )
                    ),
                ),
            )
            yield OpenAIStreamChunk(
                model="test-model",
                choices=(
                    OpenAIStreamChoice(
                        delta=OpenAIStreamDelta(),
                        finish_reason="tool_calls",
                    ),
                ),
                usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

    async def scenario() -> None:
        provider = MultiToolProvider(("both decisions applied",))
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="Two independent actions"))
            await service.wait(queued.execution_id)
            original = service.repository.resume_state(queued.execution_id)
            assert original is not None
            pending = await service.approvals(queued.execution_id)
            assert len(pending) == 2
            await service.decide(queued.execution_id, pending[0].interruption.interruption_id, "approve")
            paused_again = await service.wait(queued.execution_id)
            assert paused_again is not None and paused_again.status is ExecutionStatus.WAITING_APPROVAL
            saved = service.repository.resume_state(queued.execution_id)
            assert saved is not None
            assert saved.checkpoint.run_id == original.checkpoint.run_id
            assert saved.checkpoint.turn_id == original.checkpoint.turn_id
            assert len(saved.checkpoint.tool_results) == 1 and saved.checkpoint.tool_results[0].ok
            remaining = await service.approvals(queued.execution_id)
            assert len(remaining) == 1 and remaining[0].interruption.call_id == "second-action"
            assert (service.weave.workspace / "first.txt").read_text() == "one"
            await service.decide(queued.execution_id, remaining[0].interruption.interruption_id, "reject")
            completed = await service.wait(queued.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            turns = await service.weave.memory.session("default").read()
            results = {
                message.tool_call_id: json.loads(message.content)
                for turn in turns
                for message in turn.messages
                if isinstance(message, ToolMessage)
            }
            assert results["first-action"]["ok"] is True
            assert results["second-action"]["error_code"] == "weave.approval_denied"
            assert not (service.weave.workspace / "second.txt").exists()
            assert len(provider.requests) == 2
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_waiting_approval_survives_restart_and_resumes_the_original_run_and_turn(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        first = make_service(
            ScriptedProvider(({"name": "write_file", "arguments": {"path": "restart.txt", "content": "one action"}},))
        )
        queued = await first.submit(ExecuteRequest(objective="Remember the original objective"))
        paused = await first.wait(queued.execution_id)
        assert paused is not None and paused.status is ExecutionStatus.WAITING_APPROVAL
        saved = first.repository.resume_state(queued.execution_id)
        assert saved is not None
        waiting_graph, waiting_node = seed_graph_attempts(first, queued.execution_id)
        await first.shutdown()
        assert await first.weave.memory.session("default").active() is not None
        provider = ScriptedProvider(("resumed after restart",))
        second = make_service(provider)
        try:
            await second.start()
            assert second.weave.graph_executor.graphs.get(waiting_graph.graph_run_id) == waiting_graph
            assert second.weave.graph_executor.nodes.get(waiting_node.node_run_id) == waiting_node
            pending = await second.approvals()
            assert len(pending) == 1 and pending[0].execution_id == queued.execution_id
            await second.decide(queued.execution_id, pending[0].interruption.interruption_id, "approve")
            result = await second.wait(queued.execution_id)
            assert result is not None and result.status is ExecutionStatus.SUCCEEDED
            assert (second.weave.workspace / "restart.txt").read_text() == "one action"
            resumed = [event for event in await second.events(queued.execution_id) if event.event_type == "run.resumed"]
            assert len(resumed) == 1 and resumed[0].data["run_id"] == saved.checkpoint.run_id
            turns = await second.weave.memory.session("default").read()
            assert turns[0].turn_id == saved.checkpoint.turn_id
            assert sum(isinstance(message, UserMessage) for turn in turns for message in turn.messages) == 1
        finally:
            await second.shutdown()

    asyncio.run(scenario())


def test_restart_rejects_approval_checkpoints_that_did_not_record_child_budget_state(
    make_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        first = make_service(
            ScriptedProvider(
                ({"name": "write_file", "arguments": {"path": "legacy.txt", "content": "must not write"}},)
            )
        )
        queued = await first.submit(ExecuteRequest(objective="Wait with an old recovery contract"))
        await first.wait(queued.execution_id)
        state = first.repository.resume_state(queued.execution_id)
        assert state is not None and state.graph_budget is None
        with sqlite3.connect(first.repository.path) as connection:
            connection.execute(
                "UPDATE execution_resume_states SET state_json = ? WHERE execution_id = ?",
                (state.model_dump_json(exclude={"graph_budget"}), queued.execution_id),
            )
        await first.shutdown()
        provider = ScriptedProvider(())
        second = make_service(provider)
        try:
            with pytest.raises(SystemFault) as failure:
                await second.start()
            assert failure.value.code == "weave.system_storage"
            assert isinstance(failure.value.__cause__, ValueError)
            assert "graph_budget" in str(failure.value.__cause__)
            assert not (second.weave.workspace / "legacy.txt").exists()
            assert provider.requests == []
        finally:
            await second.shutdown()

    asyncio.run(scenario())


def test_explicit_read_approval_preserves_selected_skill_binding_and_resumes_its_body_read(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["read_file"]')

    async def scenario() -> None:
        provider = ScriptedProvider(
            (
                {
                    "name": "read_file",
                    "arguments": {
                        "scope": "skill",
                        "skill_code": "tool-development",
                        "path": "SKILL.md",
                    },
                },
                "The approved Skill body was read.",
            )
        )
        service = make_service(provider)
        skill = service.weave.workspace / "skills" / "tool-development"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: tool-development\ndescription: Develop tools\n---\n\nApproved private skill body\n"
        )
        service.weave.manifest.write_text(
            "tools: []\nskills:\n  - name: tool-development\n"
            "    code: tool-development\n    description: Develop tools\n"
        )
        try:
            queued = await service.submit(ExecuteRequest(objective="Read the selected Skill"))
            paused = await service.wait(queued.execution_id)
            assert paused is not None and paused.status is ExecutionStatus.WAITING_APPROVAL
            state = service.repository.resume_state(queued.execution_id)
            assert state is not None
            assert "Develop tools" in provider.requests[0].model_dump_json()
            assert "Approved private skill body" not in provider.requests[0].model_dump_json()
            pending = await service.approvals(queued.execution_id)
            assert len(pending) == 1 and pending[0].interruption.tool_code == "read_file"
            await service.decide(queued.execution_id, pending[0].interruption.interruption_id, "approve")
            completed = await service.wait(queued.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            assert "Approved private skill body" in provider.requests[1].model_dump_json()
            resumed = [
                event for event in await service.events(queued.execution_id) if event.event_type == "run.resumed"
            ]
            assert len(resumed) == 1 and resumed[0].data["run_id"] == state.checkpoint.run_id
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_waiting_approval_keeps_later_conversation_execution_queued_until_cancelled(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        provider = ScriptedProvider(
            (
                {"name": "write_file", "arguments": {"path": "cancelled.txt", "content": "must not write"}},
                "next objective",
            )
        )
        service = make_service(provider)
        try:
            first = await service.submit(ExecuteRequest(objective="Pause first"))
            paused = await service.wait(first.execution_id)
            assert paused is not None and paused.status is ExecutionStatus.WAITING_APPROVAL
            second = await service.submit(ExecuteRequest(objective="Wait behind approval"))
            await service.approvals(first.execution_id)
            queued = service.get(second.execution_id)
            assert queued is not None and queued.status is ExecutionStatus.QUEUED
            assert len(provider.requests) == 1
            cancelled = await service.cancel(first.execution_id)
            assert cancelled is not None and cancelled.status is ExecutionStatus.CANCELLED
            completed = await asyncio.wait_for(service.wait(second.execution_id), 2)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            assert completed.output == {"text": "next objective"}
            assert not (service.weave.workspace / "cancelled.txt").exists()
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_expired_approval_rejects_the_decision_before_any_file_action(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        provider = ScriptedProvider(
            ({"name": "write_file", "arguments": {"path": "expired.txt", "content": "must not write"}},)
        )
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="Await approval"))
            await service.wait(queued.execution_id)
            pending = await service.approvals(queued.execution_id)
            future = datetime.now(UTC) + timedelta(hours=1)

            class ExpiredClock:
                @staticmethod
                def now(tz: tzinfo | None = None) -> datetime:
                    return future

            monkeypatch.setattr(service_module, "datetime", ExpiredClock)
            with pytest.raises(ApprovalConflictError, match="deadline expired"):
                await service.decide(queued.execution_id, pending[0].interruption.interruption_id, "approve")
            record = service.get(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.FAILED
            assert record.error is not None and record.error.code == "weave.approval_expired"
            assert not (service.weave.workspace / "expired.txt").exists()
            assert await service.weave.memory.session("default").active() is None
            assert len(provider.requests) == 1
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_shutdown_during_expiry_commit_finishes_the_owned_turn_cleanup(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        service = make_service(
            ScriptedProvider(
                ({"name": "write_file", "arguments": {"path": "expired-cleanup.txt", "content": "must not write"}},)
            )
        )
        committed = asyncio.Event()
        release = ThreadEvent()
        armed = True
        loop = asyncio.get_running_loop()
        original_connect = sqlite3.connect
        future = datetime.now(UTC) + timedelta(hours=1)

        class ExpiredClock:
            @staticmethod
            def now(tz: tzinfo | None = None) -> datetime:
                return future

        class BlockingConnection(sqlite3.Connection):
            def commit(self) -> None:
                nonlocal armed
                super().commit()
                database = self.execute("PRAGMA database_list").fetchone()[2]
                if not armed or database != str(service.repository.path):
                    return
                row = self.execute("SELECT record_json FROM executions WHERE status = 'failed'").fetchone()
                if row is not None and "weave.approval_expired" in row[0]:
                    armed = False
                    loop.call_soon_threadsafe(committed.set)
                    if not release.wait(5):
                        raise TimeoutError("The test did not release the committed transition")

        def connect(database: str | Path, timeout: float = 5.0) -> sqlite3.Connection:
            return original_connect(database, timeout=timeout, factory=BlockingConnection)

        try:
            queued = await service.submit(ExecuteRequest(objective="Expire a waiting action"))
            await service.wait(queued.execution_id)
            monkeypatch.setattr(service_module, "datetime", ExpiredClock)
            monkeypatch.setattr(sqlite3, "connect", connect)
            service.arm_expiry(queued.execution_id, datetime.now(UTC) - timedelta(seconds=1))
            await asyncio.wait_for(committed.wait(), 2)
            closing = asyncio.create_task(service.shutdown())
            cancelled = asyncio.Event()
            loop.call_soon(cancelled.set)
            await cancelled.wait()
            release.set()
            await asyncio.wait_for(closing, 2)
            record = service.get(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.FAILED
            assert record.error is not None and record.error.code == "weave.approval_expired"
            assert await service.weave.memory.session("default").active() is None
            assert not (service.weave.workspace / "expired-cleanup.txt").exists()
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(scenario())


def test_restart_cleans_only_the_saved_terminal_approval_turn_before_queued_work(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        first = make_service(
            ScriptedProvider(
                ({"name": "write_file", "arguments": {"path": "crashed.txt", "content": "must not write"}},)
            )
        )
        queued = await first.submit(ExecuteRequest(objective="Crash after terminal commit"))
        paused = await first.wait(queued.execution_id)
        assert paused is not None and paused.status is ExecutionStatus.WAITING_APPROVAL
        committed = first.repository.transition(
            paused.model_copy(update={"status": ExecutionStatus.FAILED}),
            expected_revision=paused.revision,
            event=ExecutionEventInput(event_type="execution.failed"),
        )
        assert committed is not None
        await first.shutdown()
        assert await first.weave.memory.session("default").active() is not None
        second = make_service(ScriptedProvider(("queued work after cleanup",)))
        second.repository.create(
            ExecutionRecord(
                execution_id="after-crash",
                conversation_id="default",
                objective="New objective",
                status=ExecutionStatus.QUEUED,
            ),
            event=ExecutionEventInput(event_type="execution.queued"),
        )
        try:
            await second.start()
            result = await asyncio.wait_for(second.wait("after-crash"), 2)
            assert result is not None and result.status is ExecutionStatus.SUCCEEDED
            turns = await second.weave.memory.session("default").read()
            assert len(turns) == 1 and turns[0].messages[0].content == "New objective"
            assert not (second.weave.workspace / "crashed.txt").exists()
        finally:
            await second.shutdown()

    asyncio.run(scenario())


def test_approval_api_lists_exact_action_and_rejects_client_authored_results(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        provider = ScriptedProvider(
            (
                {"name": "write_file", "arguments": {"path": "api.txt", "content": "real output"}},
                "finished",
            )
        )
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="Write through API approval"))
            await service.wait(queued.execution_id)
            async with AsyncClient(transport=ASGITransport(app=create_app(service)), base_url="http://test") as client:
                pending = await client.get("/v1/executions/approvals")
                assert pending.status_code == 200
                action = pending.json()[0]
                interruption_id = action["interruption"]["interruption_id"]
                url = f"/v1/executions/{queued.execution_id}/approvals/{interruption_id}"
                invalid = await client.post(url, json={"decision": "approve", "result": {"ok": True}})
                assert invalid.status_code == 422
                accepted = await client.post(url, json={"decision": "approve"})
                assert accepted.status_code == 202
                completed = await service.wait(queued.execution_id)
                assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
                assert (service.weave.workspace / "api.txt").read_text() == "real output"
                conflict = await client.post(url, json={"decision": "reject"})
                assert conflict.status_code == 409
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_second_service_cannot_recover_or_mutate_a_live_workspace_owners_execution(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("first owner result",))
        provider.block = True
        first = make_service(provider)
        second = make_service(ScriptedProvider(()))
        try:
            queued = await first.submit(ExecuteRequest(objective="Keep original job running"))
            await provider.started.wait()
            running_graph, running_node = seed_graph_attempts(first, queued.execution_id)
            await first.start()
            assert first.weave.graph_executor.graphs.get(running_graph.graph_run_id) == running_graph
            assert first.weave.graph_executor.nodes.get(running_node.node_run_id) == running_node
            with pytest.raises(SystemFault, match="Another service owns") as failure:
                await second.start()
            assert failure.value.code == "weave.workspace_in_use"
            original = first.get(queued.execution_id)
            assert original is not None and original.status is ExecutionStatus.RUNNING
            assert await first.weave.memory.session("default").active() is not None
            assert await second.approvals() == ()
            provider.release.set()
            result = await first.wait(queued.execution_id)
            assert result is not None and result.status is ExecutionStatus.SUCCEEDED
            await first.shutdown()
            await second.start()
        finally:
            await first.shutdown()
            await second.shutdown()

    asyncio.run(scenario())


def test_repeated_cancellation_waits_for_owner_lock_acquisition_before_releasing_the_workspace(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = ThreadEvent()
    release = ThreadEvent()
    original_flock = fcntl.flock

    def gated_flock(descriptor: int, operation: int) -> None:
        entered.set()
        assert release.wait(5)
        original_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", gated_flock)

    async def scenario() -> None:
        first = make_service(ScriptedProvider(()))
        second = make_service(ScriptedProvider(()))
        starting = asyncio.create_task(first.start())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            for attempt in range(2):
                starting.cancel()
                cancelled = asyncio.Event()
                asyncio.get_running_loop().call_soon(cancelled.set)
                await cancelled.wait()
                assert starting.cancelling() == attempt + 1
                assert not starting.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await starting
            await first.shutdown()
            await second.start()
        finally:
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
            await first.shutdown()
            await second.shutdown()

    asyncio.run(scenario())


def test_cancel_queued_conversation_job_does_not_wait_for_an_unrelated_running_job(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("running job result",))
        provider.block = True
        service = make_service(provider)
        try:
            first = await service.submit(ExecuteRequest(objective="Run until explicitly released"))
            await provider.started.wait()
            second = await service.submit(ExecuteRequest(objective="Cancel this queued request"))
            cancelled = await asyncio.wait_for(service.cancel(second.execution_id), 1)
            assert cancelled is not None and cancelled.status is ExecutionStatus.CANCELLED
            running = service.get(first.execution_id)
            assert running is not None and running.status is ExecutionStatus.RUNNING
            assert await service.weave.memory.session("default").active() is not None
            assert not provider.cancelled
            provider.release.set()
            completed = await service.wait(first.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            assert len(provider.requests) == 1
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("claimed", [False, True])
def test_restart_dispatches_unclaimed_approval_but_does_not_replay_a_claimed_action(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
    claimed: bool,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        first = make_service(
            ScriptedProvider(({"name": "write_file", "arguments": {"path": "claimed.txt", "content": "execute once"}},))
        )
        queued = await first.submit(ExecuteRequest(objective="Approve original action"))
        paused = await first.wait(queued.execution_id)
        assert paused is not None and paused.status is ExecutionStatus.WAITING_APPROVAL
        recovery = first.repository.resume_state(queued.execution_id)
        assert recovery is not None
        pending_graph, pending_node = seed_graph_attempts(first, queued.execution_id)
        decision = ApprovalDecision(
            interruption_id=recovery.checkpoint.interruptions[0].interruption_id, decision="approve"
        )
        accepted = first.repository.transition(
            paused.model_copy(update={"status": ExecutionStatus.RESUME_QUEUED}),
            expected_revision=paused.revision,
            event=ExecutionEventInput(event_type="execution.resume_queued"),
            resume_state=recovery.model_copy(update={"decisions": (decision,)}),
        )
        assert accepted is not None
        if claimed:
            claimed_state = first.repository.transition(
                accepted[0].model_copy(update={"status": ExecutionStatus.RUNNING}),
                expected_revision=accepted[0].revision,
                event=ExecutionEventInput(event_type="execution.resumed"),
            )
            assert claimed_state is not None
        await first.shutdown()
        provider = ScriptedProvider(() if claimed else ("resumed",))
        second = make_service(provider)
        try:
            await second.start()
            if not claimed:
                assert second.weave.graph_executor.graphs.get(pending_graph.graph_run_id) == pending_graph
                assert second.weave.graph_executor.nodes.get(pending_node.node_run_id) == pending_node
            result = await asyncio.wait_for(second.wait(queued.execution_id), 2)
            assert result is not None
            target = second.weave.workspace / "claimed.txt"
            if claimed:
                assert result.status is ExecutionStatus.FAILED
                assert result.error is not None and result.error.code == "weave.execution_unresumable"
                assert not target.exists()
                assert provider.requests == []
            else:
                assert result.status is ExecutionStatus.SUCCEEDED
                assert target.read_text() == "execute once"
                assert len(provider.requests) == 1
        finally:
            await second.shutdown()

    asyncio.run(scenario())


def test_changed_approval_policy_fails_resume_without_running_the_original_action(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        provider = ScriptedProvider(
            ({"name": "write_file", "arguments": {"path": "changed.txt", "content": "must not write"}},)
        )
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="Use original approval binding"))
            await service.wait(queued.execution_id)
            pending = await service.approvals(queued.execution_id)
            service.weave.approval_tool_codes = frozenset()
            await service.decide(queued.execution_id, pending[0].interruption.interruption_id, "approve")
            result = await service.wait(queued.execution_id)
            assert result is not None and result.status is ExecutionStatus.FAILED
            assert result.error is not None and result.error.code == "weave.approval_incompatible"
            assert not (service.weave.workspace / "changed.txt").exists()
            assert len(provider.requests) == 1
            assert await service.weave.memory.session("default").active() is None
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_failed_approval_journal_write_rolls_back_decision_and_resume_state_together(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        service = make_service(
            ScriptedProvider(
                ({"name": "write_file", "arguments": {"path": "atomic.txt", "content": "must not write"}},)
            )
        )
        try:
            queued = await service.submit(ExecuteRequest(objective="Persist approval atomically"))
            paused = await service.wait(queued.execution_id)
            assert paused is not None and paused.status is ExecutionStatus.WAITING_APPROVAL
            state = service.repository.resume_state(queued.execution_id)
            assert state is not None
            updated = state.model_copy(
                update={
                    "decisions": (
                        ApprovalDecision(
                            interruption_id=state.checkpoint.interruptions[0].interruption_id, decision="approve"
                        ),
                    )
                }
            )
            with pytest.raises(SystemFault, match="maximum serialized size"):
                service.repository.transition(
                    paused.model_copy(update={"status": ExecutionStatus.RESUME_QUEUED}),
                    expected_revision=paused.revision,
                    resume_state=updated,
                    event=ExecutionEventInput(event_type="execution.resume_queued", data={"too_large": "x" * 20_000}),
                )
            assert service.get(queued.execution_id) == paused
            assert service.repository.resume_state(queued.execution_id) == state
            assert not (service.weave.workspace / "atomic.txt").exists()
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_graph_with_an_explicit_approval_tool_is_rejected_before_any_node_runs(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["write_file"]')

    async def scenario() -> None:
        graph: JsonObject = {"graph_id": "approval-graph", "nodes": [{"node_id": "one", "objective": "write a note"}]}
        provider = ScriptedProvider(
            (
                {"name": "submit_graph", "arguments": {"graph": graph}},
                "Cannot execute this graph yet.",
            )
        )
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="Create a graph"))
            result = await service.wait(queued.execution_id)
            assert result is not None and result.status is ExecutionStatus.SUCCEEDED
            assert service.weave.graph_store.current("approval-graph") is None
            assert "Graph nodes requiring approval are not supported" in provider.requests[1].model_dump_json()
            assert len(provider.requests) == 2
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_conversation_history_survives_a_new_service_without_persisting_dynamic_business_context(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        first = make_service(ScriptedProvider(("first answer",)))
        queued = await first.submit(ExecuteRequest(objective="first objective", input={"reference": "request-only"}))
        await first.wait(queued.execution_id)
        await first.shutdown()
        provider = ScriptedProvider(("second answer",))
        second = make_service(provider)
        try:
            queued = await second.submit(ExecuteRequest(objective="second objective"))
            await second.wait(queued.execution_id)
            payload = json.dumps(provider.requests[0].model_dump())
            assert "first objective" in payload and "first answer" in payload
            assert "request-only" not in payload
            turns = await second.weave.memory.session("default").read()
            assert len(turns) == 2 and all(turn.completed for turn in turns)
        finally:
            await second.shutdown()

    asyncio.run(scenario())


def test_cancel_waits_for_provider_cleanup_and_commits_one_terminal_event(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("unused",))
        provider.block = True
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="blocked request"))
            await provider.started.wait()
            record = await service.cancel(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.CANCELLED
            assert provider.cancelled
            events = await service.events(queued.execution_id)
            assert sum(event.event_type == "execution.cancelled" for event in events) == 1
            assert await service.weave.memory.session("default").active() is None
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_provider_fault_is_persisted_and_propagated_without_a_replacement_model(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(())
        provider.fail = True
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="failed request"))
            with pytest.raises(SystemFault, match="Provider unavailable"):
                await service.wait(queued.execution_id)
            record = service.get(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.FAILED
            assert record.error is not None and record.error.code == "model.provider_unavailable"
            assert len(provider.requests) == 1
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_graph_node_obeys_parent_allowance_and_persists_a_known_budget_failure(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {"graph_id": "limited-child", "nodes": [{"node_id": "child", "objective": "Read the note"}]}
        provider = UsageOverridesProvider(
            (
                {"name": "submit_graph", "arguments": {"graph": graph}},
                {"name": "execute_graph", "arguments": {"graph_id": "limited-child"}},
                {"name": "read_file", "arguments": {"scope": "workspace", "path": "note.txt"}},
                "The child reached its output allowance.",
            ),
            {1: 8000, 3: None},
        )
        service = make_service(provider)
        (service.weave.workspace / "note.txt").write_text("note")
        try:
            queued = await service.submit(ExecuteRequest(objective="Run within the remaining allowance"))
            result = await service.wait(queued.execution_id)
            assert result is not None and result.status is ExecutionStatus.SUCCEEDED
            assert [request.max_completion_tokens for request in provider.requests] == [8192, 192, 191, 191]
            events = await service.events(queued.execution_id)
            completed = next(event for event in events if event.event_type == "node.completed")
            record = service.weave.graph_executor.nodes.get(str(completed.data["node_run_id"]))
            assert record is not None and record.status is NodeStatus.FAILED
            assert record.sdk_run_id == completed.data["sdk_run_id"]
            assert record.sdk_run_status is RunStatus.LIMIT_REACHED
            assert record.usage is not None and record.usage.output_tokens is None
            assert record.usage.budget_output_tokens == 191
            assert record.error is not None and record.error.code == "orchestration.graph.node_limit_reached"
            assert completed.data["sdk_run_status"] == "limit_reached"
            assert completed.data["usage"] == record.usage.model_dump(mode="json")
            parent = next(
                event
                for event in events
                if event.event_type == "run.started" and event.data.get("agent_name") == "weave-supervisor"
            )
            assert service.weave.graph_executor.budget_state(str(parent.data["run_id"])) is None
            assert "orchestration.graph.node_limit_reached" in provider.requests[3].model_dump_json()
            assert "weave.node_unexpected" not in provider.requests[3].model_dump_json()
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_catalog", [False, True])
def test_graph_child_budget_survives_owner_restart_and_is_released_after_resume(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
    invalid_catalog: bool,
) -> None:
    monkeypatch.setenv("PLEIAD_APPROVAL_TOOL_CODES", '["weave.inspect_graph"]')

    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "restored-budget",
            "nodes": [{"node_id": "child", "objective": "Return an object"}],
        }
        first_provider = UsageOverridesProvider(
            (
                {"name": "submit_graph", "arguments": {"graph": graph}},
                {"name": "execute_graph", "arguments": {"graph_id": "restored-budget"}},
                '{"value":1}',
                {"name": "inspect_graph", "arguments": {"graph_id": "restored-budget"}},
            ),
            {1: 8000, 3: 190},
        )
        first = make_service(first_provider)
        queued = await first.submit(ExecuteRequest(objective="Continue the same budget after approval"))
        paused = await first.wait(queued.execution_id)
        assert paused is not None and paused.status is ExecutionStatus.WAITING_APPROVAL
        state = first.repository.resume_state(queued.execution_id)
        assert state is not None and state.graph_budget is not None
        assert state.graph_budget.remaining_output_tokens == 1
        assert first.weave.graph_executor.budget_state(state.checkpoint.run_id) is None
        assert [request.max_completion_tokens for request in first_provider.requests] == [8192, 192, 191, 191]
        await first.shutdown()
        next_provider = ScriptedProvider(
            (
                {"name": "execute_graph", "arguments": {"graph_id": "restored-budget"}},
                "The child budget is exhausted.",
                "must not be requested",
            )
        )
        second = make_service(next_provider)
        try:
            await second.start()
            pending = await second.approvals(queued.execution_id)
            if invalid_catalog:
                second.weave.manifest.write_text("tools: [")
            await second.decide(queued.execution_id, pending[0].interruption.interruption_id, "approve")
            if invalid_catalog:
                with pytest.raises(SystemFault):
                    await second.wait(queued.execution_id)
                failed = second.get(queued.execution_id)
                assert failed is not None and failed.status is ExecutionStatus.FAILED
                assert next_provider.requests == []
            else:
                result = await second.wait(queued.execution_id)
                assert result is not None and result.status is ExecutionStatus.SUCCEEDED
                assert len(next_provider.requests) == 2
                assert "orchestration.graph.child_budget_exhausted" in next_provider.requests[1].model_dump_json()
                assert [request.max_completion_tokens for request in next_provider.requests] == [190, 189]
            assert second.weave.graph_executor.budget_state(state.checkpoint.run_id) is None
        finally:
            await second.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(("revision", "expected"), [(None, 2), (1, 1), (3, None)])
def test_graph_inspection_respects_explicit_revision_without_falling_back_to_latest(
    make_service: Callable[[ScriptedProvider], ExecutionService], revision: int | None, expected: int | None
) -> None:
    async def scenario() -> None:
        arguments: JsonObject = {"graph_id": "inspected"}
        if revision is not None:
            arguments["graph_revision"] = revision
        provider = ScriptedProvider(({"name": "inspect_graph", "arguments": arguments}, "Graph inspected."))
        service = make_service(provider)
        try:
            for version in (1, 2):
                service.weave.graph_store.submit(
                    GraphSpec.model_validate(
                        {
                            "graph_id": "inspected",
                            "revision": version,
                            "nodes": [{"node_id": "write", "objective": f"Version {version}"}],
                        }
                    )
                )
            queued = await service.submit(ExecuteRequest(objective="Read the selected graph definition"))
            if expected is None:
                with pytest.raises(SystemFault):
                    await service.wait(queued.execution_id)
                failed = service.get(queued.execution_id)
                assert failed is not None and failed.status is ExecutionStatus.FAILED
                assert len(provider.requests) == 1
            else:
                completed = await service.wait(queued.execution_id)
                assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
                turns = await service.weave.memory.session("default").read()
                outputs = [
                    json.loads(message.content)
                    for turn in turns
                    for message in turn.messages
                    if isinstance(message, ToolMessage)
                ]
                assert len(outputs) == 1 and outputs[0]["ok"] is True
                assert outputs[0]["output"]["revision"] == expected
                assert outputs[0]["output"]["nodes"][0]["objective"] == f"Version {expected}"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_graph_control_tools_delegate_nodes_to_sdk_and_persist_product_results(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "example",
            "nodes": [{"node_id": "summarize", "objective": "Return a value"}],
            "outputs": [{"source_node_id": "summarize", "source_path": "/value", "target_path": "/value"}],
        }
        provider = ScriptedProvider(
            (
                {"name": "submit_graph", "arguments": {"graph": graph}},
                {"name": "execute_graph", "arguments": {"graph_id": "example"}},
                '{"value":"verified"}',
                "Graph completed.",
            )
        )
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="Run a graph"))
            record = await service.wait(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.SUCCEEDED
            assert (record.graph_id, record.graph_revision) == ("example", 1)
            assert len(provider.requests) == 4
            tool_outputs = [message.content for message in provider.requests[-1].messages if message.role == "tool"]
            assert '"verified"' in str(tool_outputs)
            events = await service.events(queued.execution_id)
            node = next(event for event in events if event.event_type == "node.completed")
            assert node.data["node_status"] == "succeeded"
            node_record = service.weave.graph_executor.nodes.get(str(node.data["node_run_id"]))
            assert node_record is not None and node_record.status.value == "succeeded"
            assert node_record.sdk_run_id == node.data["sdk_run_id"]
            graph_files = list((service.weave.workspace / "runtime" / "graph-runs").glob("*.json"))
            payload = json.loads(graph_files[0].read_text())
            assert payload["outputs"] == {"value": "verified"} and payload["status"] == "succeeded"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_new_execution_loads_new_skill_packages_without_restarting_the_service(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("first", "second"))
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="first request"))
            await service.wait(queued.execution_id)
            skill = service.weave.workspace / "skills" / "tool-development"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: tool-development\ndescription: Develop tools\n---\n\nPrivate skill instructions\n"
            )
            service.weave.manifest.write_text(
                "tools: []\nskills:\n  - name: tool-development\n"
                "    code: tool-development\n    description: Develop tools\n"
            )
            queued = await service.submit(ExecuteRequest(objective="second request"))
            await service.wait(queued.execution_id)
            payload = json.dumps(provider.requests[-1].model_dump())
            assert "Develop tools" in payload
            assert "Private skill instructions" not in payload
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_http_submission_and_replay_use_the_same_durable_execution_service(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(("HTTP answer",)))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.post("/v1/executions", json={"objective": "HTTP request"})
            assert response.status_code == 202
            execution_id = response.json()["execution"]["execution_id"]
            await service.wait(execution_id)
            response = await client.get(f"/v1/executions/{execution_id}")
            assert response.json()["execution"]["output"] == {"text": "HTTP answer"}
            response = await client.get(f"/v1/executions/{execution_id}/events?after_sequence=1")
            events = response.json()
            assert all(event["sequence"] > 1 for event in events)
            assert events[-1]["event_type"] == "execution.completed"

    asyncio.run(scenario())


def test_web_entry_serves_its_javascript_and_styles_from_the_api_origin(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.get("/")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/html")
            assert response.headers["cache-control"] == "no-cache"
            assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', response.text)
            assert assets
            for asset in assets:
                bundled = await client.get(asset)
                assert bundled.status_code == 200
                assert bundled.content
                assert not bundled.headers["content-type"].startswith("text/html")

    asyncio.run(scenario())


def test_unknown_api_paths_remain_not_found_when_the_web_ui_is_enabled(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            response = await client.get("/v1/not-an-api")
            assert response.status_code == 404
            assert response.headers["content-type"].startswith("application/json")

    asyncio.run(scenario())


def test_sdk_stream_events_update_the_tui_answer_without_a_second_message(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(("Visible answer",)))
        state = TuiState()
        state.begin_execution("default")
        projection = TuiProjection(state)

        def apply_event(event: ExecutionEvent) -> None:
            projection.apply(event)

        service.subscribe_events(apply_event)
        try:
            queued = await service.submit(ExecuteRequest(objective="UI request"))
            await service.wait(queued.execution_id)
            answers = [item for item in state.conversation_items if isinstance(item, AssistantMessageView)]
            assert len(answers) == 1 and answers[0].content == "Visible answer"
            assert state.execution_status.value == "succeeded"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_fact_recall_does_not_cross_conversation_or_user_authorization(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path, user_id="alice")
    memory.save_fact(MemoryFact(memory_id="fact", scope="conversation", scope_id="one", content="favorite tea"))
    assert len(memory.recall("one", "tea", 5)) == 1
    assert memory.recall("two", "tea", 5) == ()
    with pytest.raises(PermissionError):
        memory.save_fact(MemoryFact(memory_id="other", scope="user", scope_id="alice", content="favorite tea"))


def seed_graph_attempts(service: ExecutionService, execution_id: str) -> tuple[GraphRunRecord, NodeRunRecord]:
    """Persist interrupted product attempts with already confirmed result references."""
    graph_runner = service.weave.graph_executor
    artifact = graph_runner.artifacts.put_json({"confirmed": 7})
    graph = graph_runner.graphs.save(
        GraphRunRecord(
            graph_run_id=f"graph-{execution_id}",
            execution_id=execution_id,
            graph_id="graph",
            graph_revision=2,
            status=GraphStatus.RUNNING,
            outputs={"retained": "value"},
        )
    )
    node = graph_runner.nodes.save(
        NodeRunRecord(
            node_run_id=f"node-{execution_id}",
            execution_id=execution_id,
            graph_id="graph",
            graph_revision=2,
            node_id="node",
            sdk_run_id=f"sdk-run-{execution_id}",
            status=NodeStatus.RUNNING,
            artifact_id=artifact.artifact_id,
        )
    )
    return graph, node


@pytest.mark.parametrize("paused_attempts", [False, True])
def test_restart_fails_running_jobs_without_fabricating_a_resume(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    paused_attempts: bool,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(())
        service = make_service(provider)
        queued, _ = service.repository.create(
            ExecutionRecord(
                execution_id="orphan", conversation_id="default", objective="old request", status=ExecutionStatus.QUEUED
            ),
            event=ExecutionEventInput(event_type="execution.queued"),
        )
        service.repository.transition(
            queued.model_copy(update={"status": ExecutionStatus.RUNNING}),
            expected_revision=queued.revision,
            event=ExecutionEventInput(event_type="execution.started"),
        )
        graph, node = seed_graph_attempts(service, "orphan")
        graph_repository = service.weave.graph_executor.graphs
        node_repository = service.weave.graph_executor.nodes
        if paused_attempts:
            graph = graph_repository.save(graph.model_copy(update={"status": GraphStatus.PAUSED}))
            node = node_repository.save(node.model_copy(update={"status": NodeStatus.PAUSED}))
        finished_at = datetime.now(UTC)
        confirmed_graph = graph_repository.save(
            graph.model_copy(
                update={"graph_run_id": "confirmed-graph", "status": GraphStatus.SUCCEEDED, "finished_at": finished_at}
            )
        )
        confirmed_node = node_repository.save(
            node.model_copy(
                update={"node_run_id": "confirmed-node", "status": NodeStatus.SUCCEEDED, "finished_at": finished_at}
            )
        )
        unrelated_graph, unrelated_node = seed_graph_attempts(service, "unrelated-owner")
        try:
            await service.start()
            record = service.get("orphan")
            assert record is not None and record.status is ExecutionStatus.FAILED
            assert record.error is not None and record.error.code == "weave.execution_unresumable"
            assert provider.requests == []
            recovered_graph = graph_repository.get(graph.graph_run_id)
            recovered_node = node_repository.get(node.node_run_id)
            assert recovered_graph is not None and recovered_graph.status is GraphStatus.FAILED
            assert recovered_graph.outputs == graph.outputs
            assert recovered_graph.finished_at is not None
            assert recovered_graph.error is not None and recovered_graph.error.code == "weave.graph_execution_lost"
            assert recovered_node is not None and recovered_node.status is NodeStatus.UNKNOWN
            assert recovered_node.sdk_run_id == node.sdk_run_id
            assert recovered_node.artifact_id == node.artifact_id
            artifact_path = service.weave.graph_executor.artifacts.root / f"{node.artifact_id}.json"
            assert json.loads(artifact_path.read_text(encoding="utf-8")) == {"confirmed": 7}
            assert recovered_node.finished_at is not None
            assert recovered_node.error is not None and recovered_node.error.code == "weave.node_execution_lost"
            assert graph_repository.get(confirmed_graph.graph_run_id) == confirmed_graph
            assert node_repository.get(confirmed_node.node_run_id) == confirmed_node
            assert graph_repository.get(unrelated_graph.graph_run_id) == unrelated_graph
            assert node_repository.get(unrelated_node.node_run_id) == unrelated_node
            history = (graph_repository.list_records(), node_repository.list_records())
            await service.start()
            assert (graph_repository.list_records(), node_repository.list_records()) == history
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED])
def test_restart_settles_unfinished_attempts_when_their_execution_already_finished(
    make_service: Callable[[ScriptedProvider], ExecutionService], status: ExecutionStatus
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(())
        service = make_service(provider)
        queued, _ = service.repository.create(
            ExecutionRecord(
                execution_id="settled",
                conversation_id="default",
                objective="old request",
                status=ExecutionStatus.QUEUED,
            ),
            event=ExecutionEventInput(event_type="execution.queued"),
        )
        running = service.repository.transition(
            queued.model_copy(update={"status": ExecutionStatus.RUNNING}),
            expected_revision=queued.revision,
            event=ExecutionEventInput(event_type="execution.started"),
        )
        assert running is not None
        saved = service.repository.transition(
            running[0].model_copy(update={"status": status, "output": {"confirmed": "parent result"}}),
            expected_revision=running[0].revision,
            event=ExecutionEventInput(event_type="execution.settled"),
        )
        assert saved is not None
        graph, node = seed_graph_attempts(service, "settled")
        try:
            await service.start()
            assert service.get("settled") == saved[0]
            recovered_graph = service.weave.graph_executor.graphs.get(graph.graph_run_id)
            recovered_node = service.weave.graph_executor.nodes.get(node.node_run_id)
            assert recovered_graph is not None and recovered_graph.status is GraphStatus.FAILED
            assert recovered_graph.outputs == graph.outputs
            assert recovered_node is not None and recovered_node.status is NodeStatus.UNKNOWN
            assert recovered_node.sdk_run_id == node.sdk_run_id and recovered_node.artifact_id == node.artifact_id
            assert provider.requests == []
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("record_kind", ["graph", "node"])
def test_restart_reports_broken_attempt_records_instead_of_hiding_recovery_failure(
    make_service: Callable[[ScriptedProvider], ExecutionService], record_kind: str
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(())
        service = make_service(provider)
        queued, _ = service.repository.create(
            ExecutionRecord(
                execution_id="settled",
                conversation_id="default",
                objective="old request",
                status=ExecutionStatus.QUEUED,
            ),
            event=ExecutionEventInput(event_type="execution.queued"),
        )
        service.repository.transition(
            queued.model_copy(update={"status": ExecutionStatus.FAILED}),
            expected_revision=queued.revision,
            event=ExecutionEventInput(event_type="execution.failed"),
        )
        repository = (
            service.weave.graph_executor.graphs if record_kind == "graph" else service.weave.graph_executor.nodes
        )
        (repository.root / "broken.json").write_text('{"partial":', encoding="utf-8")
        try:
            with pytest.raises(SystemFault) as failure:
                await service.start()
            assert failure.value.code == "weave.system_storage"
            assert isinstance(failure.value.__cause__, ValueError)
            assert provider.requests == []
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_cancelled_startup_finishes_attempt_recovery_before_releasing_its_workspace_owner(
    make_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        first = make_service(ScriptedProvider(()))
        second = make_service(ScriptedProvider(()))
        queued, _ = first.repository.create(
            ExecutionRecord(
                execution_id="orphan", conversation_id="default", objective="old request", status=ExecutionStatus.QUEUED
            ),
            event=ExecutionEventInput(event_type="execution.queued"),
        )
        first.repository.transition(
            queued.model_copy(update={"status": ExecutionStatus.RUNNING}),
            expected_revision=queued.revision,
            event=ExecutionEventInput(event_type="execution.started"),
        )
        graph, node = seed_graph_attempts(first, "orphan")
        loop = asyncio.get_running_loop()
        replacing = asyncio.Event()
        release = ThreadEvent()
        original_replace = Path.replace
        armed = True

        def wait_before_replacing(source: Path, target: str | Path) -> Path:
            nonlocal armed
            if armed and source.name == f"{graph.graph_run_id}.json.tmp":
                armed = False
                loop.call_soon_threadsafe(replacing.set)
                if not release.wait(5.0):
                    raise TimeoutError("test did not release the attempt replacement")
            return original_replace(source, target)

        monkeypatch.setattr(Path, "replace", wait_before_replacing)
        starting = asyncio.create_task(first.start())
        try:
            await asyncio.wait_for(replacing.wait(), 3.0)
            for cancellation in range(2):
                starting.cancel()
                delivered = asyncio.Event()
                loop.call_soon(delivered.set)
                await delivered.wait()
                assert not starting.done(), f"startup returned before recovery after cancellation {cancellation}"
            with pytest.raises(SystemFault, match="Another service owns"):
                await second.start()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await starting
            recovered_graph = first.weave.graph_executor.graphs.get(graph.graph_run_id)
            recovered_node = first.weave.graph_executor.nodes.get(node.node_run_id)
            assert recovered_graph is not None and recovered_graph.status is GraphStatus.FAILED
            assert recovered_node is not None and recovered_node.status is NodeStatus.UNKNOWN
            await first.shutdown()
            await second.start()
            settled = second.get("orphan")
            assert settled is not None and settled.status is ExecutionStatus.FAILED
        finally:
            release.set()
            if not starting.done():
                starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
            await first.shutdown()
            await second.shutdown()

    asyncio.run(scenario())


class ScriptProcess:
    def __init__(self, stdout: bytes = b'{"count":3}', exit_code: int = 0) -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.requests: list[tuple[tuple[str, ...], bytes | None]] = []
        self.removed = False

    async def __call__(
        self, command: tuple[str, ...], *, stdin: bytes | None, output_limit: int, timeout_seconds: float | None
    ) -> ProcessResult:
        self.requests.append((command, stdin))
        if command[1] == "info":
            return ProcessResult(0, b"", b"", False, 0.0, False)
        if command[1] == "create":
            return ProcessResult(0, b"container-id\n", b"", False, 0.0, False)
        if command[1] == "start":
            return ProcessResult(0, self.stdout, b"script diagnostics", False, 0.0, False)
        if command[1] == "rm":
            self.removed = True
            return ProcessResult(0, b"container-id\n", b"", False, 0.0, False)
        if command[1] == "inspect":
            return (
                ProcessResult(1, b"", b"Error: No such object\n", False, 0.0, False)
                if self.removed
                else ProcessResult(0, str(self.exit_code).encode("ascii"), b"", False, 0.0, False)
            )
        raise AssertionError(command)


def test_workspace_tool_runs_through_sdk_oci_without_importing_user_code(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        tools = service.weave.workspace / "tools"
        tools.mkdir()
        (tools / "count.py").write_text('raise RuntimeError("Host must never import this file")\n')
        (tools / "count.json").write_text(
            json.dumps(
                {
                    "script": "count.py",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object", "required": ["count"]},
                }
            )
        )
        service.weave.manifest.write_text(
            "tools:\n  - name: count\n    code: custom.count\n    description: Count items\nskills: []\n"
        )
        process = ScriptProcess()
        service.weave.sandbox.backend.process_executor = process
        service.weave.sandbox.backend.requires_runtime_binary = False
        try:
            snapshot = await service.weave.capabilities()
            tool = snapshot.tools.load("custom.count")
            output = await tool.execute(ToolContext(call_id="call"), {"items": [1, 2, 3]})
            assert output == {"count": 3}
            assert process.removed
            start = next(request for request in process.requests if request[0][1] == "start")
            assert start[1] == b'{"items": [1, 2, 3]}'
            create = next(request[0] for request in process.requests if request[0][1] == "create")
            assert create[-1] == "/workspace/tools/count.py"
            assert create[create.index("--entrypoint") + 1] == "python"
            assert create[create.index("--network") + 1] == "none"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_the_owned_execution(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("completed after waiter left",))
        provider.block = True
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="continue independently"))
            await provider.started.wait()
            waiter = asyncio.create_task(service.wait(queued.execution_id))
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            provider.release.set()
            record = await service.wait(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.SUCCEEDED
            assert not provider.cancelled
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_queued_execution_can_be_cancelled_before_its_sdk_task_starts(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(())
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="cancel before start"))
            record = await service.cancel(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.CANCELLED
            assert provider.requests == []
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_graph_cycles_are_rejected_before_a_product_revision_is_saved(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "cycle",
            "nodes": [{"node_id": "one", "objective": "one"}, {"node_id": "two", "objective": "two"}],
            "edges": [
                {"edge_id": "a", "source_node_id": "one", "target_node_id": "two"},
                {"edge_id": "b", "source_node_id": "two", "target_node_id": "one"},
            ],
        }
        provider = ScriptedProvider(({"name": "submit_graph", "arguments": {"graph": graph}}, "Rejected the cycle."))
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="check cyclic graph"))
            await service.wait(queued.execution_id)
            result = next(message.content for message in provider.requests[-1].messages if message.role == "tool")
            assert isinstance(result, str)
            envelope = json.loads(result)
            assert envelope["ok"] is True
            payload = envelope["output"]
            assert payload["accepted"] is False
            assert any(item["code"] == "cycle" for item in payload["diagnostics"])
            assert service.weave.graph_store.current("cycle") is None
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_graph_node_business_failure_is_not_rendered_as_success(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "invalid-output",
            "nodes": [
                {
                    "node_id": "node",
                    "objective": "Return valid output",
                    "output_schema": {"type": "object", "required": ["value"]},
                }
            ],
        }
        provider = ScriptedProvider(
            (
                {"name": "submit_graph", "arguments": {"graph": graph}},
                {"name": "execute_graph", "arguments": {"graph_id": "invalid-output"}},
                "{}",
                "The node failed validation.",
            )
        )
        service = make_service(provider)
        state = TuiState()
        projection = TuiProjection(state)

        def apply_event(event: ExecutionEvent) -> None:
            projection.apply(event)

        service.subscribe_events(apply_event)
        try:
            queued = await service.submit(ExecuteRequest(objective="validate graph outputs"))
            await service.wait(queued.execution_id)
            events = await service.events(queued.execution_id)
            node = next(event for event in events if event.event_type == "node.completed")
            assert node.data["node_status"] == "failed"
            assert state.graph.nodes["node"].status == "failed"
            outputs = [message.content for message in provider.requests[-1].messages if message.role == "tool"]
            assert '"failed"' in str(outputs)
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_terminal_execution_cannot_reenter_running_state(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(("done",)))
        try:
            queued = await service.submit(ExecuteRequest(objective="finish once"))
            record = await service.wait(queued.execution_id)
            assert record is not None
            with pytest.raises(ValueError, match="Invalid Execution transition"):
                service.repository.transition(
                    record.model_copy(update={"status": ExecutionStatus.RUNNING}),
                    expected_revision=record.revision,
                    event=ExecutionEventInput(event_type="execution.started"),
                )
            assert service.get(record.execution_id) == record
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_cli_runs_the_shared_service_and_returns_its_durable_result(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service(ScriptedProvider(("CLI answer",)))
    monkeypatch.setattr(cli, "create_application", lambda: service)
    result = CliRunner().invoke(cli.app, ["run", "CLI request"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "succeeded" and payload["output"] == {"text": "CLI answer"}
    assert service.closed


def test_textual_app_mounts_with_current_sdk_model_metadata_and_renders_the_result(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(("TUI answer",)))
        app = PleiadApp(service)
        async with app.run_test(size=(120, 40)):
            app.state.begin_execution("default")
            app.state.add_user_message("TUI request")
            await app.execute("TUI request")
            assert app.state.execution_status.value == "succeeded"
            assert app.state.model_context.model_name == "test-model"
            answers = [item.content for item in app.state.conversation_items if isinstance(item, AssistantMessageView)]
            assert answers == ["TUI answer"]
        assert service.closed

    asyncio.run(scenario())


def test_streamed_sdk_events_keep_the_originating_span_when_consumed_by_the_product_task(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(("answer",)))
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        telemetry = Observability(tracer_provider=provider)
        service.observability = telemetry
        service.weave.observability = telemetry
        try:
            queued = await service.submit(ExecuteRequest(objective="trace this request"))
            await service.wait(queued.execution_id)
            spans = exporter.get_finished_spans()
            execution = next(span for span in spans if span.name == "weave.execution")
            event = next(
                event for event in await service.events(queued.execution_id) if event.event_type == "model.completed"
            )
            sdk_span = next(
                span for span in spans if span.context is not None and f"{span.context.span_id:016x}" == event.span_id
            )
            assert execution.context is not None and sdk_span.context is not None
            assert sdk_span.name.startswith("pleiad.")
            assert sdk_span.context.span_id != execution.context.span_id
            assert event.trace_id == f"{execution.context.trace_id:032x}"
        finally:
            await service.shutdown()
            provider.shutdown()

    asyncio.run(scenario())


def test_restart_cleans_the_orphaned_sdk_turn_before_scheduling_a_queued_conversation_request(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(("recovered queue",)))
        queued, _ = service.repository.create(
            ExecutionRecord(
                execution_id="orphan", conversation_id="default", objective="old request", status=ExecutionStatus.QUEUED
            ),
            event=ExecutionEventInput(event_type="execution.queued"),
        )
        service.repository.transition(
            queued.model_copy(update={"status": ExecutionStatus.RUNNING}),
            expected_revision=queued.revision,
            event=ExecutionEventInput(event_type="execution.started"),
        )
        await service.weave.memory.session("default").begin("weave-supervisor", (UserMessage(content="old request"),))
        service.repository.create(
            ExecutionRecord(
                execution_id="queued", conversation_id="default", objective="new request", status=ExecutionStatus.QUEUED
            ),
            event=ExecutionEventInput(event_type="execution.queued"),
        )
        try:
            await service.start()
            record = await service.wait("queued")
            assert record is not None and record.output == {"text": "recovered queue"}
            assert record.status is ExecutionStatus.SUCCEEDED
            turns = await service.weave.memory.session("default").read()
            assert len(turns) == 1 and turns[0].messages[0].content == "new request"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("stdout,exit_code", [(b"not-json", 0), (b"{}", 0), (b'{"count":3}', 1)])
def test_workspace_tool_rejects_invalid_results_and_preserves_process_failure_diagnostics(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    stdout: bytes,
    exit_code: int,
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        tools = service.weave.workspace / "tools"
        tools.mkdir()
        (tools / "count.py").write_text("print('{}')\n")
        (tools / "count.json").write_text(
            json.dumps(
                {
                    "script": "count.py",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object", "required": ["count"]},
                }
            )
        )
        service.weave.manifest.write_text(
            "tools:\n  - name: count\n    code: custom.count\n    description: Count items\nskills: []\n"
        )
        process = ScriptProcess(stdout, exit_code)
        service.weave.sandbox.backend.process_executor = process
        service.weave.sandbox.backend.requires_runtime_binary = False
        try:
            tool = (await service.weave.capabilities()).tools.load("custom.count")
            if exit_code:
                result = await tool.execute(ToolContext(call_id="call"), {})
                assert isinstance(result, ToolFailure)
                assert result.error.code == "weave.workspace_tool_failed"
                assert result.output["exit_code"] == 1 and result.output["stderr"] == "script diagnostics"
            else:
                with pytest.raises(ToolError) as failure:
                    await tool.execute(ToolContext(call_id="call"), {})
                assert failure.value.code == "weave.workspace_tool_output"
                assert failure.value.__cause__ is not None
            assert process.removed
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_log_files_redact_credentials_in_messages_and_exception_causes(tmp_path: Path) -> None:
    configure_logging(LoggingConfig(directory=tmp_path, console=False))
    try:
        error = ValueError("Authorization: Bearer private-key")
        logging.getLogger("pleiad_weave").error(
            "provider error token=private-token", exc_info=(ValueError, error, None)
        )
    finally:
        shutdown_logging()
    content = (tmp_path / "weave.log").read_text()
    assert "private-key" not in content and "private-token" not in content
    assert "[REDACTED]" in content and "ValueError" in content


@pytest.mark.parametrize("parameter", ["key", "ak", "KeY", "AK"])
def test_map_http_failures_redact_query_credentials_but_preserve_diagnostics(tmp_path: Path, parameter: str) -> None:
    request = Request("GET", f"https://maps.example/mcp?{parameter}=private-map-key&mode=driving&ak=second-key")
    response = Response(401, request=request)
    configure_logging(LoggingConfig(directory=tmp_path, console=False))
    try:
        try:
            response.raise_for_status()
        except HTTPStatusError:
            logging.getLogger("pleiad.mcp.discovery").error(
                f"MCP Tool discovery failed server=maps url={request.url}", exc_info=True
            )
    finally:
        shutdown_logging()

    content = (tmp_path / "weave.log").read_text()
    assert "private-map-key" not in content and "second-key" not in content
    assert f"https://maps.example/mcp?{parameter}=[REDACTED]&mode=driving&ak=[REDACTED]" in content
    assert "401" in content and "HTTPStatusError" in content
    assert "Traceback (most recent call last):" in content


def test_opaque_graph_ids_cannot_escape_the_revision_storage_root(tmp_path: Path) -> None:
    root = tmp_path / "graphs"
    store = TaskGraphStore(root)
    spec = GraphSpec.model_validate({"graph_id": "..", "nodes": [{"node_id": "one", "objective": "one"}]})
    store.submit(spec)
    restored = TaskGraphStore(root).current("..")
    assert restored is not None and restored.graph == spec
    paths = list(tmp_path.rglob("revision-*.json"))
    assert len(paths) == 1 and paths[0].is_relative_to(root)
    assert json.loads(paths[0].read_text())["graph"]["graph_id"] == ".."


def test_node_artifact_failure_settles_product_attempt_as_unknown(
    make_service: Callable[[ScriptedProvider], ExecutionService], tmp_path: Path
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        graph_executor = service.weave.graph_executor
        unavailable = tmp_path / "blocked-artifacts"
        unavailable.write_text("not a directory", encoding="utf-8")
        graph_executor.artifacts.root = unavailable

        async def complete(input_value: JsonObject) -> JsonObject:
            return {"answer": "completed"}

        executor = NodeExecutor(
            "execution",
            "graph",
            1,
            graph_executor.nodes,
            graph_executor.artifacts,
            service.weave.publisher,
            graph_run_id="graph-run",
        )
        try:
            with pytest.raises(SystemFault) as failure:
                await executor.execute(Node(node_id="node", handler=complete), {})
            assert failure.value.code == "weave.system_storage"
            assert isinstance(failure.value.__cause__, NotADirectoryError)
            records = tuple(graph_executor.nodes.root.glob("*.json"))
            assert len(records) == 1
            record = json.loads(records[0].read_text(encoding="utf-8"))
            assert record["status"] == NodeStatus.UNKNOWN.value
            assert record["finished_at"] is not None
            assert record["error"]["code"] == "weave.system_storage"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_workspace_catalog_rejects_a_skill_package_symlink(
    make_service: Callable[[ScriptedProvider], ExecutionService], tmp_path: Path
) -> None:
    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        package = tmp_path / "external"
        package.mkdir()
        (package / "SKILL.md").write_text(
            "---\nname: external\ndescription: External package\n---\nInstructions", encoding="utf-8"
        )
        skills = service.weave.workspace / "skills"
        skills.mkdir()
        (skills / "external").symlink_to(package, target_is_directory=True)
        service.weave.manifest.write_text(
            "tools: []\nskills:\n  - name: external\n    code: external\n    description: External package\n",
            encoding="utf-8",
        )
        try:
            with pytest.raises(ValueError, match="symlink"):
                await service.weave.capabilities()
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_large_answer_succeeds_without_overflowing_the_lifecycle_journal(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        answer = "answer " * 3_000
        service = make_service(ScriptedProvider((answer,)))
        live: list[ExecutionEvent] = []
        service.subscribe_events(live.append)
        try:
            queued = await service.submit(ExecuteRequest(objective="Produce a long answer"))
            record = await service.wait(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.SUCCEEDED
            assert record.output == {"text": answer}
            completed = next(event for event in live if event.event_type == "answer.completed")
            assert completed.data["text"] == answer
            replay = await service.events(queued.execution_id)
            durable = next(event for event in replay if event.event_type == "answer.completed")
            assert durable.event_id == completed.event_id
            assert durable.sequence == completed.sequence
            assert durable.data == {"output_location": "execution"}
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_product_answer_succeeds_with_unknown_usage_in_its_journal(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(("answer",), report_usage=False)
        service = make_service(provider)
        try:
            queued = await service.submit(ExecuteRequest(objective="answer"))
            record = await service.wait(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.SUCCEEDED
            assert record.output == {"text": "answer"}
            events = await service.events(queued.execution_id)
            completed = next(event for event in events if event.event_type == "model.completed")
            assert completed.data["input_tokens"] is None
            assert completed.data["output_tokens"] is None
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_product_does_not_replace_an_answer_when_unknown_usage_exhausts_run_budget(
    make_service: Callable[[ScriptedProvider], ExecutionService],
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            (
                {"name": "read_file", "arguments": {"path": "note.txt", "scope": "workspace"}},
                "must not be requested",
            ),
            report_usage=False,
        )
        service = make_service(provider)
        (service.weave.workspace / "note.txt").write_text("note", encoding="utf-8")
        try:
            queued = await service.submit(ExecuteRequest(objective="read the note"))
            record = await service.wait(queued.execution_id)
            assert record is not None and record.status is ExecutionStatus.FAILED
            assert record.error is not None and record.error.code == "weave.run_limit_reached"
            assert len(provider.requests) == 1
            assert await service.weave.memory.session(queued.conversation_id).active() is None
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_enabled_mcp_tools_are_searchable_and_execute_through_the_sdk(
    make_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote = FastMCP("external")
    observed: list[tuple[str, dict[str, str], str | WorkspaceOAuth | None]] = []

    @remote.tool
    def echo(value: str) -> dict[str, str]:
        return {"value": value}

    @remote.tool
    def count(value: str) -> dict[str, int]:
        return {"length": len(value)}

    def transport(url: str, headers: dict[str, str], auth: str | WorkspaceOAuth | None) -> FastMCPTransport:
        observed.append((url, headers, auth))
        return FastMCPTransport(remote)

    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", transport)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TEST_MCP_TOKEN=file-token\nTEST_MCP_API_KEY=file-api-key\n", encoding="utf-8")
    monkeypatch.setenv("TEST_MCP_TOKEN", "process-token")
    monkeypatch.delenv("TEST_MCP_API_KEY", raising=False)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.weave.workspace.parent / "mcp.json").write_text(
            json.dumps(
                {
                    "servers": [
                        {
                            "code": "remote",
                            "enabled": True,
                            "url": "https://tools.example/mcp",
                            "auth": "bearer",
                            "token_env": "TEST_MCP_TOKEN",
                            "headers": {"X-Client": "weave"},
                            "header_env": {"apiKey": "TEST_MCP_API_KEY"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        try:
            snapshot = await service.weave.capabilities()
            assert {item.code for item in snapshot.tools.search("echo")} >= {"mcp.remote.echo"}
            first = await snapshot.tools.load("mcp.remote.echo").execute(ToolContext(call_id="echo"), {"value": "abc"})
            second = await snapshot.tools.load("mcp.remote.count").execute(
                ToolContext(call_id="count"), {"value": "abc"}
            )
            assert isinstance(first, dict) and first["structured_content"] == {"value": "abc"}
            assert isinstance(second, dict) and second["structured_content"] == {"length": 3}
            assert observed == [
                ("https://tools.example/mcp", {"X-Client": "weave", "apiKey": "file-api-key"}, "process-token")
            ]
            async with AsyncClient(transport=ASGITransport(app=create_app(service)), base_url="http://test") as client:
                response = await client.get("/v1/resources")
                assert response.status_code == 200
                assert "mcp.remote.echo" in {item["code"] for item in response.json()["tools"]}
                assert "process-token" not in response.text
                assert "file-api-key" not in response.text
                assert "tools.example" not in response.text
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("allowed_tools", [("read_record",), ()])
def test_mcp_allowlist_exposes_only_selected_remote_tools(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
    allowed_tools: tuple[str, ...],
) -> None:
    remote = FastMCP("read-only")

    @remote.tool
    def read_record() -> str:
        return "record"

    @remote.tool
    def write_record() -> str:
        raise AssertionError("An excluded MCP tool must not be executed")

    def transport(url: str, headers: dict[str, str], auth: str | WorkspaceOAuth | None) -> FastMCPTransport:
        return FastMCPTransport(remote)

    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", transport)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.weave.workspace.parent / "mcp.json").write_text(
            McpConfiguration(
                servers=(
                    McpConnection(
                        code="remote", enabled=True, url="https://tools.example/mcp", allowed_tools=allowed_tools
                    ),
                )
            ).model_dump_json(),
            encoding="utf-8",
        )
        try:
            snapshot = await service.weave.capabilities()
            assert {code for code in snapshot.tools.codes() if code.startswith("mcp.")} == {
                f"mcp.remote.{name}" for name in allowed_tools
            }
            if allowed_tools:
                result = await snapshot.tools.load("mcp.remote.read_record").execute(ToolContext(call_id="read"), {})
                assert isinstance(result, dict) and result["structured_content"] == {"result": "record"}
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_mcp_allowlist_reports_unknown_tool_names_before_exposing_capabilities(
    make_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FastMCP("empty")

    def transport(url: str, headers: dict[str, str], auth: str | WorkspaceOAuth | None) -> FastMCPTransport:
        return FastMCPTransport(remote)

    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", transport)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.weave.workspace.parent / "mcp.json").write_text(
            '{"servers":[{"code":"remote","enabled":true,"url":"https://tools.example/mcp",'
            '"allowed_tools":["missing_tool"]}]}',
            encoding="utf-8",
        )
        try:
            with pytest.raises(
                ValueError, match="MCP server remote allowed_tools names unknown remote tools: missing_tool"
            ):
                await service.weave.capabilities()
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_stdio_mcp_uses_explicit_host_environment_and_executes_tools(
    make_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote = FastMCP("stdio")
    observed: list[tuple[str, list[str], dict[str, str], bool]] = []

    @remote.tool
    def echo(value: str) -> dict[str, str]:
        return {"value": value}

    def process_transport(command: str, args: list[str], env: dict[str, str], *, keep_alive: bool) -> FastMCPTransport:
        observed.append((command, args, env, keep_alive))
        return FastMCPTransport(remote)

    monkeypatch.setattr(stdio_module, "StdioTransport", process_transport)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TEST_STDIO_TOKEN=file-token\nTEST_STDIO_REGION=local-region\n", encoding="utf-8")
    monkeypatch.setenv("TEST_STDIO_TOKEN", "process-token")
    monkeypatch.setenv("TEST_UNRELATED_SECRET", "must-not-be-forwarded")
    monkeypatch.delenv("TEST_STDIO_REGION", raising=False)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.weave.workspace.parent / "mcp.json").write_text(
            McpConfiguration(
                servers=(
                    McpConnection(
                        code="local",
                        enabled=True,
                        command="external-server",
                        args=("--stdio",),
                        env={"MODE": "stdio"},
                        env_from={"API_KEY": "TEST_STDIO_TOKEN", "REGION": "TEST_STDIO_REGION"},
                    ),
                )
            ).model_dump_json(),
            encoding="utf-8",
        )
        try:
            snapshot = await service.weave.capabilities()
            result = await snapshot.tools.load("mcp.local.echo").execute(
                ToolContext(call_id="stdio"), {"value": "ready"}
            )
            assert isinstance(result, dict) and result["structured_content"] == {"value": "ready"}
            assert (
                observed
                == [
                    (
                        "external-server",
                        ["--stdio"],
                        {"MODE": "stdio", "API_KEY": "process-token", "REGION": "local-region"},
                        False,
                    )
                ]
                * 2
            )
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_disabled_mcp_presets_do_not_require_credentials_or_connect(
    make_service: Callable[[ScriptedProvider], ExecutionService], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise AssertionError("Disabled MCP presets must not construct a network transport")

    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", unavailable)
    monkeypatch.setattr(mcp_module, "IsolatedStdioTransport", unavailable)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.weave.workspace.parent / "mcp.json").write_text(
            '{"servers":[{"code":"disabled","url_env":"UNCONFIGURED_TEST_URL","auth":"bearer",'
            '"token_env":"UNCONFIGURED_TEST_TOKEN"},'
            '{"code":"disabled-stdio","command":"missing-server",'
            '"env_from":{"API_KEY":"UNCONFIGURED_TEST_TOKEN"}}]}',
            encoding="utf-8",
        )
        try:
            assert not any(code.startswith("mcp.") for code in (await service.weave.capabilities()).tools.codes())
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "connection",
    [
        {"url": "https://tools.example/mcp", "auth": "bearer", "token_env": "UNCONFIGURED_TEST_TOKEN"},
        {"url": "https://tools.example/mcp", "header_env": {"apiKey": "UNCONFIGURED_TEST_TOKEN"}},
        {"command": "missing-server", "env_from": {"API_KEY": "UNCONFIGURED_TEST_TOKEN"}},
    ],
)
def test_enabled_mcp_connection_reports_missing_credentials_before_discovery(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    connection: dict[str, object],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("UNCONFIGURED_TEST_TOKEN", raising=False)

    async def scenario() -> None:
        service = make_service(ScriptedProvider(()))
        (service.weave.workspace.parent / "mcp.json").write_text(
            json.dumps({"servers": [{"code": "remote", "enabled": True, **connection}]}),
            encoding="utf-8",
        )
        try:
            with pytest.raises(ValueError, match="UNCONFIGURED_TEST_TOKEN"):
                await service.weave.capabilities()
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "servers",
    [
        [{"code": "duplicate", "url": "https://example.com/mcp"}] * 2,
        [{"code": "remote", "url": "file:///tmp/mcp"}],
        [{"code": "remote", "url": "https://example.com", "url_env": "OTHER_URL"}],
        [{"code": "remote", "url": "https://example.com", "auth": "bearer"}],
        [{"code": "remote", "url": "https://example.com", "headers": {"Authorization": "Bearer secret"}}],
        [{"code": "remote", "url": "https://example.com", "header_env": {"aUthorization": "TOKEN"}}],
        [
            {
                "code": "remote",
                "url": "https://example.com",
                "headers": {"X-Key": "public"},
                "header_env": {"x-key": "KEY"},
            }
        ],
        [{"code": "remote", "url": "https://example.com", "headers": {"X-Key": "public", "x-key": "duplicate"}}],
        [{"code": "remote", "url": "https://example.com", "command": "uvx"}],
        [{"code": "remote", "command": " "}],
        [{"code": "remote", "command": "uvx", "auth": "oauth"}],
        [{"code": "remote", "command": "uvx", "headers": {"X-Client": "weave"}}],
        [{"code": "remote", "command": "uvx", "header_env": {"apiKey": "KEY"}}],
        [{"code": "remote", "url": "https://example.com", "args": ["server"]}],
        [{"code": "remote", "url": "https://example.com", "env_from": {"KEY": "TOKEN"}}],
        [{"code": "remote", "command": "uvx", "env": {"KEY": "public"}, "env_from": {"KEY": "TOKEN"}}],
        [{"code": "remote", "command": "uvx", "env_from": {"KEY": "bad name"}}],
        [{"code": "remote", "url": "https://example.com", "allowed_tools": ["read", "read"]}],
        [{"code": "remote", "url": "https://example.com", "allowed_tools": [" "]}],
    ],
)
def test_invalid_mcp_configuration_is_rejected(servers: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError):
        McpConfiguration.model_validate({"servers": servers})


def test_mcp_oauth_requires_terminal_login_instead_of_opening_a_browser(tmp_path: Path) -> None:
    auth = WorkspaceOAuth("https://example.com/mcp", tmp_path / "tokens", interactive=False)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="pleiad mcp-login"):
            await auth.redirect_handler("https://example.com/consent")

    asyncio.run(scenario())


def test_mcp_bearer_credentials_can_be_loaded_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_MCP_TOKEN", raising=False)
    (tmp_path / ".env").write_text("TEST_MCP_TOKEN=file-token\n", encoding="utf-8")
    variable_name = "TEST_MCP_TOKEN"
    server = create_mcp_server(
        McpConnection(code="remote", url="https://example.com/mcp", auth="bearer", token_env=variable_name),
        tmp_path,
    )
    assert isinstance(server.client.transport, StreamableHttpTransport)
    assert server.client.transport.url == "https://example.com/mcp"
    assert server.client.transport.auth is not None


@pytest.mark.parametrize(
    ("skill_code", "instruction"),
    [
        ("systematic-debugging", "Do not propose a fix before collecting evidence of the root cause."),
        ("verification-before-completion", "Evidence must precede a completion claim."),
        ("travel-planning", "查询时有库存也不等于已预订。"),
        ("web-research", "搜索摘要仅是线索。"),
        ("document-writing", "不能以截断内容重建整篇文档。"),
    ],
)
def test_default_workspace_graph_reads_selected_skill_instructions_without_remote_services(
    make_service: Callable[[ScriptedProvider], ExecutionService],
    monkeypatch: pytest.MonkeyPatch,
    skill_code: str,
    instruction: str,
) -> None:
    repository = Path(__file__).resolve().parents[3]
    source = repository / "workspace"
    monkeypatch.setenv("PLEIAD_MCP_CONFIG_FILE", str(repository / "mcp.json"))
    configuration = load_mcp_configuration(source)
    assert configuration.servers
    assert all(not item.enabled for item in configuration.servers)

    class SkillReadingProvider(ScriptedProvider):
        async def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
            if len(self.requests) == 3:
                observed = request.messages[-1]
                assert observed.role == "tool" and isinstance(observed.content, str)
                envelope = json.loads(observed.content)
                assert envelope["ok"] is True
                output = envelope["output"]
                assert output["scope"] == "skill" and output["path"] == "SKILL.md"
                assert isinstance(output["content"], str)
                self.responses = iter(
                    (json.dumps({"skill_content": output["content"]}), "The selected skill instructions were read.")
                )
            async for chunk in super().stream(request):
                yield chunk

    async def scenario() -> None:
        graph: JsonObject = {
            "graph_id": "read-selected-skill",
            "nodes": [
                {
                    "node_id": "review",
                    "objective": "Read the selected skill and return its complete instructions as evidence",
                    "skill_codes": [skill_code],
                }
            ],
        }
        provider = SkillReadingProvider(
            (
                {"name": "submit_graph", "arguments": {"graph": graph}},
                {"name": "execute_graph", "arguments": {"graph_id": "read-selected-skill"}},
                {
                    "name": "read_file",
                    "arguments": {"scope": "skill", "skill_code": skill_code, "path": "SKILL.md"},
                },
            )
        )
        service = make_service(provider)
        shutil.copytree(source / "skills", service.weave.workspace / "skills")
        shutil.copytree(source / "tools", service.weave.workspace / "tools")
        shutil.copyfile(source / "capabilities.yaml", service.weave.manifest)
        expected_content = (source / "skills" / skill_code / "SKILL.md").read_text(encoding="utf-8")
        try:
            snapshot = await service.weave.capabilities()
            assert not any(code.startswith("mcp.") for code in snapshot.tools.codes())
            assert not snapshot.skills.contains("webapp-testing")
            queued = await service.submit(ExecuteRequest(objective="Use the selected skill in a graph node"))
            completed = await service.wait(queued.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.SUCCEEDED
            first_node_input = "\n".join(
                message.content for message in provider.requests[2].messages if isinstance(message.content, str)
            )
            assert f"# Selected skills\n\n- `{skill_code}`:" in first_node_input
            assert instruction not in first_node_input
            tool_content = provider.requests[3].messages[-1].content
            assert isinstance(tool_content, str)
            observation = json.loads(tool_content)
            assert observation["output"]["content"] == expected_content
            assert instruction in observation["output"]["content"]
            node_runs = service.weave.graph_executor.nodes.list_records()
            assert len(node_runs) == 1 and node_runs[0].status is NodeStatus.SUCCEEDED
            artifact_id = node_runs[0].artifact_id
            assert artifact_id is not None
            artifact = service.weave.graph_executor.artifacts.root / f"{artifact_id}.json"
            assert json.loads(artifact.read_text(encoding="utf-8")) == {"skill_content": expected_content}
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_mcp_oauth_state_cannot_be_stored_in_the_tool_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLEIAD_MCP_AUTH_DIRECTORY", str(tmp_path / "credentials"))
    with pytest.raises(ValueError, match="outside the tool-visible workspace"):
        create_mcp_server(McpConnection(code="remote", url="https://example.com/mcp", auth="oauth"), tmp_path)


def test_mcp_configuration_cannot_be_read_from_the_tool_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLEIAD_MCP_CONFIG_FILE", str(tmp_path / "mcp.json"))
    (tmp_path / "mcp.json").write_text('{"servers":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="outside the tool-visible workspace"):
        load_mcp_configuration(tmp_path)


def test_mcp_login_checks_a_disabled_preset_without_starting_models_or_running_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FastMCP("login")
    actions: list[str] = []

    @remote.tool
    def write_record(value: str) -> str:
        actions.append(value)
        return value

    def transport(url: str, headers: dict[str, str], auth: str | WorkspaceOAuth | None) -> FastMCPTransport:
        return FastMCPTransport(remote)

    monkeypatch.setattr(mcp_module, "StreamableHttpTransport", transport)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLEIAD_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("PLEIAD_MCP_CONFIG_FILE", str(tmp_path / "mcp.json"))
    (tmp_path / "mcp.json").write_text(
        '{"servers":[{"code":"remote","url":"https://example.com/mcp"}]}', encoding="utf-8"
    )
    result = CliRunner().invoke(cli.app, ["mcp-login", "remote"])
    assert result.exit_code == 0, result.output
    assert "MCP connection ready: remote (1 tools)" in result.output
    assert actions == []
